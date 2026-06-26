"""Environment variables + credentials pushed into a sandbox at creation.

Some commands an agent runs inside the sandbox need secrets/env to work —
a GitHub token for `git push`, an API key for a vendor CLI, a registry
login. Those values have to be present in the sandbox's OWN environment,
set when the sandbox is created. This is distinct from:
  - the agent process's env (the agent shouldn't leak its own secrets), and
  - the model API key (that's the agent's, not the sandboxed command's).

This module is the BACKEND-AGNOSTIC half: it declares WHICH env vars to
push and resolves their VALUES. Three sources, lowest→highest precedence:
  1. passthrough  — copy named vars from the agent process env
                    (`ADK_CC_SANDBOX_ENV_PASSTHROUGH=GITHUB_TOKEN,HF_TOKEN`)
  2. static       — literal KEY=VALUE pairs
                    (`ADK_CC_SANDBOX_ENV=TZ=UTC,LANG=C.UTF-8` or JSON)
  3. credentials  — per-tenant secrets from the CredentialProvider, mapped
                    ENV_NAME=credential_key
                    (`ADK_CC_SANDBOX_ENV_CREDENTIALS=GITHUB_TOKEN=gh_pat`)

Each sandbox backend maps the resolved `dict[str, str]` onto its own
creation API. DaytonaBackend (the first consumer) puts it in the
`POST /api/sandbox` `env` field, where Daytona bakes it as the sandbox's
container environment — inherited by every subsequent `exec`. Other
backends adopt the SAME spec by applying the resolved dict at their own
create step:
  - DockerBackend          → container `environment` / `-e` flags
  - E2BBackend             → `Sandbox(envs=...)`
  - SandboxServiceBackend  → the upstream session-create `env` field

Resolution runs at `ensure_workspace()` (per session), so a rotated secret
takes effect on the next session — same lifecycle as MCP/token creds.
Values are NEVER logged; only key names are.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Optional

if TYPE_CHECKING:
    from ..credentials import CredentialProvider

_log = logging.getLogger(__name__)


def _parse_kv(raw: Optional[str], *, what: str) -> dict[str, str]:
    """Parse `KEY=VALUE,KEY2=VALUE2` or a JSON object into a dict.

    JSON form (`{"K": "V"}`) is the escape hatch for values containing
    commas/equals. The list form splits on commas, then on the FIRST `=`
    (so values may contain `=`). Empty input → empty dict.
    """
    if not raw:
        return {}
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"{what}: invalid JSON object: {e}") from e
        if not isinstance(obj, dict):
            raise ValueError(f"{what}: JSON must be an object of KEY→VALUE")
        return {str(k): str(v) for k, v in obj.items()}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(
                f"{what}: entry {pair!r} is not KEY=VALUE (use JSON form for "
                f"values with commas)"
            )
        k, v = pair.split("=", 1)
        k = k.strip()
        if not k:
            raise ValueError(f"{what}: empty key in entry {pair!r}")
        out[k] = v
    return out


@dataclass(frozen=True)
class SandboxEnvSpec:
    """Declarative set of env vars to push into a sandbox at creation.

    Immutable + side-effect-free; `resolve()` does the per-tenant lookup.
    Construct directly (tests/embedding) or via `sandbox_env_spec_from_env`.
    """

    # Literal KEY→VALUE pairs, pushed verbatim.
    static: Mapping[str, str] = field(default_factory=dict)
    # Names of vars to COPY from the agent process env (if set there).
    passthrough: tuple[str, ...] = ()
    # ENV_NAME → credential key. The value is resolved per-tenant from the
    # CredentialProvider at resolve() time. Secrets live here, not in env.
    credentials: Mapping[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.static or self.passthrough or self.credentials)

    async def resolve(
        self,
        *,
        tenant_id: str,
        user_id: Optional[str] = None,
        credentials: Optional["CredentialProvider"] = None,
        host_env: Optional[Mapping[str, str]] = None,
    ) -> dict[str, str]:
        """Resolve to a concrete `{name: value}` dict for injection.

        Precedence (later overrides earlier): passthrough < static <
        credentials. Missing sources are skipped with a WARNING (never
        fatal) — a sandbox should still come up if one optional secret
        isn't registered yet. Values are never logged.
        """
        host = os.environ if host_env is None else host_env
        env: dict[str, str] = {}

        # 1) passthrough — copy from the agent process env when present.
        for name in self.passthrough:
            val = host.get(name)
            if val is None:
                _log.warning(
                    "sandbox_env: passthrough var %r is not set in the agent "
                    "environment — not pushed to the sandbox",
                    name,
                )
                continue
            env[name] = val

        # 2) static literals.
        for k, v in self.static.items():
            env[str(k)] = str(v)

        # 3) per-tenant credentials (highest precedence — secrets win).
        for env_name, cred_key in self.credentials.items():
            if credentials is None:
                _log.warning(
                    "sandbox_env: credential %r→%r requested but no "
                    "CredentialProvider is available (static-token / dev mode) "
                    "— %r not pushed. Use a provider for per-tenant secrets.",
                    cred_key,
                    env_name,
                    env_name,
                )
                continue
            val = await credentials.get(
                tenant_id=tenant_id, key=cred_key, user_id=user_id or None
            )
            if val is None:
                _log.warning(
                    "sandbox_env: credential key %r is not registered for "
                    "tenant %r — env var %r not pushed",
                    cred_key,
                    tenant_id,
                    env_name,
                )
                continue
            env[env_name] = val

        if env:
            _log.info(
                "sandbox_env: resolved %d env var(s) for tenant %r: %s",
                len(env),
                tenant_id,
                sorted(env),  # KEY NAMES ONLY — never values
            )
        return env


def sandbox_env_spec_from_env(
    environ: Optional[Mapping[str, str]] = None,
) -> SandboxEnvSpec:
    """Build a `SandboxEnvSpec` from the `ADK_CC_SANDBOX_ENV*` knobs.

      ADK_CC_SANDBOX_ENV              static literals  (KEY=VALUE,… or JSON)
      ADK_CC_SANDBOX_ENV_PASSTHROUGH  comma list of host env var NAMES
      ADK_CC_SANDBOX_ENV_CREDENTIALS  ENV_NAME=credential_key,…  (or JSON)

    Backend-agnostic: every backend that supports creation-time env reads
    the same knobs, so the operator configures the sandbox environment once
    regardless of which backend is active. Returns an empty spec (is_empty)
    when none are set — callers then skip env injection entirely.
    """
    e = os.environ if environ is None else environ
    static = _parse_kv(e.get("ADK_CC_SANDBOX_ENV"), what="ADK_CC_SANDBOX_ENV")
    passthrough = tuple(
        x.strip()
        for x in (e.get("ADK_CC_SANDBOX_ENV_PASSTHROUGH") or "").split(",")
        if x.strip()
    )
    creds = _parse_kv(
        e.get("ADK_CC_SANDBOX_ENV_CREDENTIALS"),
        what="ADK_CC_SANDBOX_ENV_CREDENTIALS",
    )
    return SandboxEnvSpec(
        static=static, passthrough=passthrough, credentials=creds
    )
