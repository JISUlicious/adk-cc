"""SecretRedactionPlugin — keep resolved secret values out of model I/O, the
session store, and anything delivered to the user (Phase 6 hygiene gate).

A resolved secret value should live in exactly one flow:
`CredentialProvider → resolver → sandbox exec env / MCP auth header`. This
plugin is the defense-in-depth egress scrub: it knows the current session
user's secret values (resolved user-over-tenant from the CredentialProvider,
TTL-cached) and replaces any occurrence with `‹redacted:NAME›` at the two
boundaries that feed the model / the persisted events / the user:

  - after_tool_callback  → tool RESULTS (e.g. `run_bash` stdout echoing a token,
    an `env` dump, a stack trace). Scrubbing here runs BEFORE ADK returns the
    result to the model and persists it as an event, so the raw value never
    enters the context, the session DB, or the UI stream.
  - after_model_callback → the model RESPONSE text (belt-and-suspenders; the
    model shouldn't have a value since it never sees one, but partials are
    streamed to the user).

Limitations (documented, not bugs): substring redaction can miss a value split
across streamed chunks or transformed beyond exact + base64; and it cannot stop
a MALICIOUS skill that deliberately exfiltrates a secret (that's the skill-trust
/ sandbox-network boundary). It stops ACCIDENTAL exposure.

Inert when no CredentialProvider is configured or the session has no principal.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

_log = logging.getLogger(__name__)

_DEFAULT_TTL_S = 15.0


def _ttl_s() -> float:
    try:
        return max(0.0, float(os.environ.get("ADK_CC_SECRET_REDACTION_TTL_S", "")))
    except ValueError:
        return _DEFAULT_TTL_S


class SecretRedactionPlugin(BasePlugin):
    def __init__(self, credentials: Any = None, *, name: str = "adk_cc_secret_redaction") -> None:
        super().__init__(name=name)
        self._credentials = credentials  # Optional[CredentialProvider]
        self._ttl = _ttl_s()
        # (tenant_id, user_id) -> (expiry_monotonic, {value: placeholder})
        self._cache: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}

    # ---- principal + value resolution -------------------------------------
    @staticmethod
    def _principal(state) -> Optional[tuple[str, str]]:
        from ..service.tenancy import _STATE_TENANT_KEY

        try:
            tc = state.get(_STATE_TENANT_KEY)
        except Exception:  # noqa: BLE001
            return None
        if tc is None:
            return None
        tid = getattr(tc, "tenant_id", None)
        uid = getattr(tc, "user_id", None)
        if not tid:
            return None
        return tid, (uid or "")

    async def _value_map(self, state) -> dict[str, str]:
        """Resolve {secret_value: '‹redacted:NAME›'} for this session's user,
        user-over-tenant, TTL-cached. Empty when off / no secrets."""
        if self._credentials is None:
            return {}
        pr = self._principal(state)
        if pr is None:
            return {}
        tenant_id, user_id = pr
        now = time.monotonic()
        hit = self._cache.get(pr)
        if hit and hit[0] > now:
            return hit[1]
        mapping: dict[str, str] = {}
        try:
            names = set(await self._credentials.list_keys(tenant_id=tenant_id))
            if user_id:
                names |= set(
                    await self._credentials.list_keys(tenant_id=tenant_id, user_id=user_id)
                )
            for name in names:
                val = await self._credentials.get(
                    tenant_id=tenant_id, key=name, user_id=user_id or None
                )
                if val:
                    mapping[val] = f"‹redacted:{name}›"
        except Exception as e:  # noqa: BLE001 — redaction must never break a turn
            _log.debug("secret value resolution failed (%s: %s)", type(e).__name__, e)
            return self._cache.get(pr, (0.0, {}))[1]
        self._cache[pr] = (now + self._ttl, mapping)
        return mapping

    def invalidate(self) -> None:
        self._cache.clear()

    # ---- scrubbing --------------------------------------------------------
    @staticmethod
    def _scrub_text(s: str, mapping: dict[str, str]) -> str:
        for val, ph in mapping.items():
            if val and val in s:
                s = s.replace(val, ph)
            try:
                b64 = base64.b64encode(val.encode("utf-8")).decode("ascii")
            except Exception:  # noqa: BLE001
                b64 = ""
            if b64 and b64 in s:
                s = s.replace(b64, ph)
        return s

    @classmethod
    def _scrub_inplace(cls, obj: Any, mapping: dict[str, str]) -> None:
        """Recursively scrub strings WITHIN a dict/list, mutating containers in
        place. We mutate (not replace) so we can return None from the callback
        and NOT short-circuit ADK's other after_tool plugins (audit, mcp-export)
        — see ordering note in __init__. Top-level immutable str can't be
        scrubbed in place; tool results are dicts, so that's not a real case."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str):
                    obj[k] = cls._scrub_text(v, mapping)
                else:
                    cls._scrub_inplace(v, mapping)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                if isinstance(v, str):
                    obj[i] = cls._scrub_text(v, mapping)
                else:
                    cls._scrub_inplace(v, mapping)

    # ---- hooks ------------------------------------------------------------
    # Both hooks MUTATE IN PLACE and return None: ADK stops at the first plugin
    # that returns non-None, so returning the scrubbed object would skip audit /
    # trace / mcp-export. Registered BEFORE those plugins so they observe the
    # already-scrubbed content.
    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> Optional[dict]:
        mapping = await self._value_map(getattr(tool_context, "state", None))
        if mapping and isinstance(result, dict):
            self._scrub_inplace(result, mapping)
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> Optional[LlmResponse]:
        mapping = await self._value_map(getattr(callback_context, "state", None))
        if not mapping:
            return None
        content = getattr(llm_response, "content", None)
        parts = getattr(content, "parts", None) if content is not None else None
        for part in parts or []:
            txt = getattr(part, "text", None)
            if isinstance(txt, str) and txt:
                new = self._scrub_text(txt, mapping)
                if new != txt:
                    part.text = new
        return None
