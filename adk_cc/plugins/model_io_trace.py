"""Log raw model request and response for debugging model behavior.

When debugging "why did the model emit this tool call" or "what did
we actually send", the existing tool-level audit isn't enough — the
audit logs that a tool was called, but not the chat history,
system prompt, or tool schemas that drove the model's decision.

This plugin hooks ADK's `before_model_callback` and
`after_model_callback`, dumps the `LlmRequest` / `LlmResponse`
contents, and emits them as both:

  - A DEBUG log line under `adk_cc.plugins.model_io_trace` (good for
    live tail / stderr).
  - An audit event (`model_request` / `model_response`) — durable
    JSONL when an AuditPlugin sink is configured.

Both paths share the same opt-in env var. The plugin is wired into
the agent unconditionally but exits early in `__init__` when the
env var isn't `1`, so the per-turn callback overhead is a single
attribute lookup.

## Why opt-in

Raw model I/O can be enormous. A typical adk-cc turn carries:

  - Tens of tool declarations with full JSON schemas (run_bash,
    write_file, all the skills, MCP tools, ...).
  - Full conversation history (the entire `contents` list ADK
    builds, after compaction).
  - System prompt + per-agent instructions.

Easily multi-MB per request. Default-on would balloon stderr, kill
audit JSONL disk budgets, and obscure the structured debugging
signal we already added in the previous logging PR. Default OFF;
flip on for the debugging session that actually needs it.

## Truncation

Each direction's payload is JSON-serialized then truncated at
`max_bytes` (default 50KB, env-configurable). Truncated payloads
get a `"truncated": true` marker and the original byte count so
operators know to bump the cap or look elsewhere.

## Streaming

`after_model_callback` fires once per streamed chunk when the
backend is streaming. The plugin skips records where
`response.partial is True` so only the final aggregated response
ends up in the trail. The model's intermediate chunks are visible
in ADK's own event stream; the trail captures the model's
final output, which is what the audit consumer wants.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin

from .audit import emit_audit_event, is_audit_enabled

_log = logging.getLogger(__name__)

# Env knobs — read at plugin __init__ so toggling them mid-run
# requires an agent restart (matches every other env-driven plugin
# in this repo).
_ENV_ENABLED = "ADK_CC_LOG_MODEL_IO"
_ENV_MAX_BYTES = "ADK_CC_LOG_MODEL_IO_MAX_BYTES"
_DEFAULT_MAX_BYTES = 50_000


class ModelIOTracePlugin(BasePlugin):
    """Dumps `LlmRequest` and `LlmResponse` payloads for debugging.

    Opt-in via `ADK_CC_LOG_MODEL_IO=1`. When off, every callback is
    a single env-flag-attribute check and a `return None` — zero
    serialization cost. When on, payloads are JSON-serialized,
    truncated at `ADK_CC_LOG_MODEL_IO_MAX_BYTES` (default 50KB),
    and emitted as a DEBUG log line + a `model_request` /
    `model_response` audit event.
    """

    def __init__(
        self,
        *,
        name: str = "adk_cc_model_io_trace",
        max_bytes: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        super().__init__(name=name)
        # Tests pass explicit kwargs; runtime reads env. The kwargs
        # win when set so the test surface doesn't depend on env state.
        self._enabled = (
            enabled
            if enabled is not None
            else os.environ.get(_ENV_ENABLED) == "1"
        )
        if max_bytes is not None:
            self._max_bytes = max_bytes
        else:
            try:
                self._max_bytes = int(
                    os.environ.get(_ENV_MAX_BYTES, _DEFAULT_MAX_BYTES)
                )
            except ValueError:
                self._max_bytes = _DEFAULT_MAX_BYTES

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        if not self._enabled:
            return None
        payload = _dump_request(llm_request, self._max_bytes)
        _log.debug(
            "model_request bytes=%s tool_count=%s content_turns=%s",
            payload.get("payload_bytes"),
            payload.get("tool_count"),
            payload.get("content_turns"),
            extra=payload,
        )
        if is_audit_enabled():
            event: dict[str, Any] = {
                "ts": time.time(),
                "event": "model_request",
                **payload,
            }
            event.update(_ctx_fields(callback_context))
            emit_audit_event(event)
        # Return None so we don't short-circuit the model call.
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> Optional[LlmResponse]:
        if not self._enabled:
            return None
        # ADK fires this once per streamed chunk when the backend
        # streams. Skip partials — only the final aggregated response
        # is interesting for the trail; intermediate chunks are visible
        # in ADK's own event stream when you need them.
        if getattr(llm_response, "partial", False):
            return None
        payload = _dump_response(llm_response, self._max_bytes)
        _log.debug(
            "model_response bytes=%s parts=%s error_code=%s",
            payload.get("payload_bytes"),
            payload.get("parts_count"),
            payload.get("error_code"),
            extra=payload,
        )
        if is_audit_enabled():
            event: dict[str, Any] = {
                "ts": time.time(),
                "event": "model_response",
                **payload,
            }
            event.update(_ctx_fields(callback_context))
            emit_audit_event(event)
        return None


# --- Serialization helpers -----------------------------------------


def _dump_request(req: LlmRequest, max_bytes: int) -> dict[str, Any]:
    """Build a JSON-serializable dict from `LlmRequest`. Captures the
    contents (chat history), the model config (tools list, generation
    params), and a few summary stats so a quick scan tells you how big
    the turn was."""
    contents = _safe_dump(getattr(req, "contents", None))
    config = _safe_dump(getattr(req, "config", None))
    model = getattr(req, "model", None)
    tool_count = 0
    if isinstance(config, dict):
        tools = config.get("tools") or []
        if isinstance(tools, list):
            tool_count = sum(
                len(t.get("function_declarations") or [])
                if isinstance(t, dict)
                else 0
                for t in tools
            )

    payload = {
        "model": model,
        "contents": contents,
        "config": config,
    }
    serialized = _truncate_json(payload, max_bytes)
    return {
        "model": model,
        "tool_count": tool_count,
        "content_turns": len(contents) if isinstance(contents, list) else 0,
        "payload_bytes": serialized["bytes"],
        "truncated": serialized["truncated"],
        "payload": serialized["text"],
    }


def _dump_response(resp: LlmResponse, max_bytes: int) -> dict[str, Any]:
    """Build a JSON-serializable dict from `LlmResponse`. Captures the
    content (model output), error fields if any, and parts-count."""
    content = _safe_dump(getattr(resp, "content", None))
    parts_count = 0
    if isinstance(content, dict):
        parts = content.get("parts") or []
        if isinstance(parts, list):
            parts_count = len(parts)

    payload = {
        "content": content,
        "error_code": getattr(resp, "error_code", None),
        "error_message": getattr(resp, "error_message", None),
        "partial": getattr(resp, "partial", None),
    }
    serialized = _truncate_json(payload, max_bytes)
    return {
        "parts_count": parts_count,
        "error_code": getattr(resp, "error_code", None),
        "error_message": getattr(resp, "error_message", None),
        "payload_bytes": serialized["bytes"],
        "truncated": serialized["truncated"],
        "payload": serialized["text"],
    }


def _safe_dump(obj: Any) -> Any:
    """Best-effort serialization. Pydantic models (ADK uses them for
    LlmRequest/Response) have model_dump; fall back to repr if not."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json", exclude_none=True)
        except Exception:
            pass
    if isinstance(obj, list):
        return [_safe_dump(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _safe_dump(v) for k, v in obj.items()}
    # Bytes / non-JSON-serializable types fall back to repr in the
    # `default` callback of json.dumps below.
    return obj


def _truncate_json(obj: Any, max_bytes: int) -> dict[str, Any]:
    """Serialize to JSON, return {text, bytes, truncated}. When the
    payload exceeds `max_bytes`, the text is cut at the boundary and
    the marker is set — operators can bump the cap or look elsewhere."""
    try:
        full = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception as e:
        # Last-resort fallback so we never crash a model call to log it.
        return {
            "text": f"<serialization failed: {type(e).__name__}: {e}>",
            "bytes": 0,
            "truncated": False,
        }
    nbytes = len(full.encode("utf-8"))
    if nbytes <= max_bytes:
        return {"text": full, "bytes": nbytes, "truncated": False}
    # UTF-8-safe cut: encode, slice, decode with errors='ignore' so a
    # multi-byte boundary doesn't produce a UnicodeDecodeError.
    cut = full.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return {"text": cut, "bytes": nbytes, "truncated": True}


def _ctx_fields(ctx: CallbackContext) -> dict[str, Any]:
    """Pull session_id / invocation_id / agent_name from the callback
    context (best-effort; tests pass mocks)."""
    out: dict[str, Any] = {}
    for field in ("agent_name", "invocation_id"):
        try:
            v = getattr(ctx, field, None)
            if v is not None:
                out[field] = v
        except Exception:
            pass
    try:
        sess = getattr(ctx, "session", None)
        if sess is not None:
            sid = getattr(sess, "id", None)
            if sid is not None:
                out["session_id"] = sid
    except Exception:
        pass
    return out
