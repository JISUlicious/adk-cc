"""Model-endpoint config + a JSON-file registry with an active pointer.

A "model endpoint" is one OpenAI-compatible backend the agent can talk to:
a model id, an api_base, and a reference to the api key. The registry holds
a NAMED set of endpoints plus which one is ACTIVE, persisted to a single
JSON file (`ADK_CC_MODEL_REGISTRY_FILE`) so a switch survives restart.

Secret handling: the api key is NOT stored inline. `api_key_env` names an
environment variable holding the key (matching the static-MCP convention).
The registry/admin layer therefore never persists or returns a raw key.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class ModelEndpointConfig(BaseModel):
    """One OpenAI-compatible model backend."""

    name: str = Field(description="Unique logical name (the registry id).")
    model: str = Field(description="LiteLLM model id, e.g. openai/Qwen3-...")
    api_base: str = Field(description="OpenAI-compatible base URL.")
    api_key_env: str = Field(
        default="ADK_CC_API_KEY",
        description=(
            "Name of the env var holding the api key for this endpoint. The "
            "key itself is never stored in the registry or returned over HTTP. "
            "Set to an empty string for an intentionally keyless endpoint "
            "(e.g. a local model server that needs no auth)."
        ),
    )
    max_tokens: Optional[int] = Field(
        default=None,
        description=(
            "Optional output-token cap per call (litellm max_tokens). Prevents "
            "the model stopping mid tool-call when the server's default output "
            "limit is low — the root cause behind truncated tool-call JSON. "
            "Falls back to ADK_CC_MAX_OUTPUT_TOKENS, then uncapped."
        ),
    )

    def masked(self) -> dict:
        """JSON-safe dict for API responses: includes whether the key env var
        is currently resolvable, but never the key value."""
        d = self.model_dump(mode="json")
        d["api_key_present"] = self.api_key_present()
        return d

    def requires_key(self) -> bool:
        """True when this endpoint declares an api_key_env (i.e. it expects a
        key). An empty api_key_env means 'intentionally keyless'."""
        return bool(self.api_key_env)

    def api_key_present(self) -> bool:
        """True when the declared key env var actually resolves in the
        environment. Trivially True for an intentionally-keyless endpoint."""
        if not self.requires_key():
            return True
        return bool(os.environ.get(self.api_key_env))

    def resolve_api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


class _RegistryFile(BaseModel):
    """On-disk shape: the endpoint set + the active name."""

    endpoints: list[ModelEndpointConfig] = Field(default_factory=list)
    active: Optional[str] = None


class ModelEndpointRegistry:
    """Thread-safe JSON-file store of model endpoints + the active pointer.

    All mutations persist immediately. Reads are cheap (re-read the file so
    multiple workers stay consistent — same rationale as the tenant
    registry). A process-level lock guards read-modify-write races within a
    worker; cross-worker safety relies on the single-writer admin path.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    # -- persistence ----------------------------------------------------

    def _read(self) -> _RegistryFile:
        if not self._path.exists():
            return _RegistryFile()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _RegistryFile()
        return _RegistryFile.model_validate(raw)

    def _write(self, data: _RegistryFile) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(data.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # -- queries --------------------------------------------------------

    def list(self) -> list[ModelEndpointConfig]:
        return self._read().endpoints

    def active_name(self) -> Optional[str]:
        return self._read().active

    def get_active(self) -> Optional[ModelEndpointConfig]:
        data = self._read()
        if data.active is None:
            return None
        for e in data.endpoints:
            if e.name == data.active:
                return e
        return None

    def get(self, name: str) -> Optional[ModelEndpointConfig]:
        for e in self._read().endpoints:
            if e.name == name:
                return e
        return None

    # -- mutations ------------------------------------------------------

    def upsert(self, cfg: ModelEndpointConfig) -> None:
        """Add or replace an endpoint by name. The first endpoint added
        becomes active automatically."""
        with self._lock:
            data = self._read()
            data.endpoints = [e for e in data.endpoints if e.name != cfg.name]
            data.endpoints.append(cfg)
            if data.active is None:
                data.active = cfg.name
            self._write(data)

    def remove(self, name: str) -> None:
        """Remove an endpoint. Refuses to remove the LAST endpoint or the
        ACTIVE one (deactivate/switch first) — raises ValueError."""
        with self._lock:
            data = self._read()
            if not any(e.name == name for e in data.endpoints):
                return
            if len(data.endpoints) <= 1:
                raise ValueError("cannot remove the last model endpoint")
            if data.active == name:
                raise ValueError(
                    "cannot remove the active endpoint; activate another first"
                )
            data.endpoints = [e for e in data.endpoints if e.name != name]
            self._write(data)

    def activate(self, name: str) -> None:
        """Make `name` the active endpoint.

        Raises ValueError if the endpoint is unknown, or if its api_key_env
        is declared but unset in the server process — activating an endpoint
        whose key is missing would only surface as an opaque provider auth
        error on the next user message, so we reject it here at config time.
        """
        with self._lock:
            data = self._read()
            target = next((e for e in data.endpoints if e.name == name), None)
            if target is None:
                raise ValueError(f"unknown endpoint: {name!r}")
            if target.requires_key() and not target.api_key_present():
                raise ValueError(
                    f"cannot activate {name!r}: its api_key_env "
                    f"{target.api_key_env!r} is not set in the server "
                    f"environment. Set it first (or clear api_key_env for a "
                    f"keyless endpoint)."
                )
            data.active = name
            self._write(data)

    def seed_default(self, cfg: ModelEndpointConfig) -> None:
        """Ensure at least one endpoint exists: if the registry is empty,
        add `cfg` and make it active. Idempotent — no-op once populated.
        Lets the boot model (from env) appear in the panel as endpoint #1."""
        with self._lock:
            data = self._read()
            if data.endpoints:
                return
            data.endpoints = [cfg]
            data.active = cfg.name
            self._write(data)
