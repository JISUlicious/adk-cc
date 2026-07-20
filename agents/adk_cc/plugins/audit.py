"""Audit plugin — observes every tool call.

Routes through ADK's existing plugin surface directly:
  - `before_tool_callback`  → records the attempt (including denied)
  - `after_tool_callback`   → records the result (only fires on executed)
  - `on_tool_error_callback`→ records errors

All callbacks return None so audit observes without mutating the chain.

Plugin chain ordering matters: register `AuditPlugin` BEFORE
`PermissionPlugin`. That way audit's `before_tool_callback` fires on the
attempt before permission's potential short-circuit; both denied and
allowed attempts get logged. Audit's `after_tool_callback` only fires
when execution actually happened — correct, since denied calls have no
result to record.

Sink is pluggable: either a path (JSONL) or a callable. Operators wanting
Postgres / DataDog / SQS write a callable that takes the event dict.

## Event schema (versioned by `event` field)

  - `tool_call_attempt`     — before permission check
  - `tool_call_result`      — after execution
  - `tool_call_error`       — execution raised
  - `permission_decision`   — decide() outcome (allow/deny/ask + rule)
  - `state_mutation`        — permission_mode flip or allow-rule write
  - `confirmation_resume`   — user response received and applied
  - `model_request`         — full LlmRequest dump (ModelIOTracePlugin)
  - `model_response`        — full LlmResponse dump (ModelIOTracePlugin)
  - `compaction_triggered`  — before LlmEventSummarizer fires
  - `compaction_success`    — summarizer returned a non-None event
  - `compaction_failure`    — summarizer returned None / raised / timed out
  - `project_context_loaded`— CLAUDE.md / CONTEXT.md files loaded into
                              the system_instruction (first load + on
                              every mtime drift)

Events beyond the first three are emitted from inside other plugins /
tools / wrappers via the module-level `emit_audit_event` helper. The
helper looks up the process-wide AuditPlugin instance set at agent
construction (see `set_global_sink`). Callsites that fire when no
AuditPlugin is configured are silent no-ops.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

# NOTE: `..tools.base.AdkCcTool` is imported lazily inside `_tool_fields`
# rather than at module-load. `tools/base.py` imports this module
# (`emit_confirmation_resume`), so an eager import here would form a
# circular import: tools.base → plugins.audit → tools.base.

# Process-wide sink registered by the AuditPlugin instance at __init__.
# Callsites in other modules (engine.decide, permissions._add_session_allow,
# plan-mode tools' state writes, confirmation resume) emit through here
# instead of holding a direct reference to the plugin instance. When no
# plugin is configured, `_GLOBAL_SINK` stays None and emits are no-ops.
_GLOBAL_SINK: Optional[Callable[[dict[str, Any]], None]] = None

Sink = Union[str, Path, Callable[[dict], None]]
"""Where audit events go.

  - str/Path: JSONL file. Created on first write; one event per line.
  - callable(event_dict): operator-defined sink (e.g. Postgres insert).
"""


def _default_sink_path() -> Path:
    override = os.environ.get("ADK_CC_AUDIT_LOG")
    if override:
        return Path(override)
    from .. import deployment as _dep

    return _dep.data_dir() / "audit.jsonl"


class AuditPlugin(BasePlugin):
    def __init__(
        self,
        sink: Optional[Sink] = None,
        *,
        name: str = "adk_cc_audit",
    ) -> None:
        super().__init__(name=name)
        self._sink_callable: Optional[Callable[[dict], None]] = None
        self._sink_path: Optional[Path] = None
        if sink is None:
            self._sink_path = _default_sink_path()
        elif callable(sink):
            self._sink_callable = sink
        else:
            self._sink_path = Path(sink)
        # Register this instance as the process-wide sink so non-plugin
        # callsites (engine.decide, _add_session_allow, plan-mode tools)
        # can route audit events here via `emit_audit_event`. Last
        # instance wins — operators registering a second AuditPlugin
        # are responsible for ordering. Unit tests that build many
        # instances should call `clear_global_sink()` between cases
        # or accept that the latest one is active.
        set_global_sink(self._emit)

    def _emit(self, event: dict[str, Any]) -> None:
        if self._sink_callable is not None:
            try:
                self._sink_callable(event)
            except Exception:
                # Audit must never raise — losing a record is preferable to
                # crashing the agent loop.
                pass
            return

        assert self._sink_path is not None
        try:
            self._sink_path.parent.mkdir(parents=True, exist_ok=True)
            with self._sink_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception:
            pass

    @staticmethod
    def _ctx_fields(ctx: ToolContext) -> dict[str, Any]:
        # Tolerant accessors — different ADK invocation paths populate
        # different fields, and unit tests pass mocks.
        out: dict[str, Any] = {}
        for field in ("agent_name", "invocation_id", "function_call_id", "user_id"):
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

    @staticmethod
    def _tool_fields(tool: BaseTool) -> dict[str, Any]:
        # Deferred import — see module docstring NOTE.
        from ..tools.base import AdkCcTool
        out: dict[str, Any] = {"tool_name": tool.name}
        if isinstance(tool, AdkCcTool):
            out["tool_meta"] = tool.meta.model_dump()
        return out

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        self._emit(
            {
                "ts": time.time(),
                "event": "tool_call_attempt",
                **self._tool_fields(tool),
                "tool_args": tool_args,
                **self._ctx_fields(tool_context),
            }
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: Any,
    ) -> Optional[dict]:
        # ADK's after_tool_callback signature types `result` as `dict`, but
        # in practice tools can return strings (MCP tools, some BaseTool
        # impls). Defensive: only pull `status` when the result is dict-shaped.
        result_status = result.get("status") if isinstance(result, dict) else None
        self._emit(
            {
                "ts": time.time(),
                "event": "tool_call_result",
                **self._tool_fields(tool),
                "tool_args": tool_args,
                "result_status": result_status,
                **self._ctx_fields(tool_context),
            }
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> Optional[dict]:
        self._emit(
            {
                "ts": time.time(),
                "event": "tool_call_error",
                **self._tool_fields(tool),
                "tool_args": tool_args,
                "error_type": type(error).__name__,
                "error_message": str(error),
                **self._ctx_fields(tool_context),
            }
        )
        return None


# --- Module-level emit helpers for non-plugin callsites ---------------
#
# Permission-decision logging, state-mutation logging, and confirmation
# resume logging fire from inside ADK plugin callbacks / tool methods
# that don't hold a reference to the AuditPlugin instance. Rather than
# weaving the plugin through every layer, callsites use these helpers;
# the AuditPlugin registers its sink at __init__.
#
# If no plugin is registered, `emit_audit_event` is a no-op — operators
# who haven't configured audit get zero overhead.


def set_global_sink(sink: Callable[[dict[str, Any]], None]) -> None:
    """Register the process-wide audit sink. Called by AuditPlugin.__init__.
    Tests can call directly to install a mock without instantiating the
    plugin."""
    global _GLOBAL_SINK
    _GLOBAL_SINK = sink


def clear_global_sink() -> None:
    """Test helper — drop the registered sink. Use between tests that
    construct AuditPlugin instances to avoid leakage."""
    global _GLOBAL_SINK
    _GLOBAL_SINK = None


def emit_audit_event(event: dict[str, Any]) -> None:
    """Send a structured event to the registered audit sink. No-op
    when no AuditPlugin has been constructed in this process.

    The caller is responsible for setting `event["event"]` to one of
    the documented event types and including a `ts` timestamp.

    Caller-side guard: most callsites should still gate themselves with
    `if not is_audit_enabled(): return` to skip expensive payload
    construction when audit is off. This helper handles the no-sink
    case but doesn't help with prep-work cost."""
    sink = _GLOBAL_SINK
    if sink is None:
        return
    try:
        sink(event)
    except Exception:
        # Audit must never raise — losing a record is preferable to
        # crashing the agent loop. Mirrors the same fail-silent
        # discipline as AuditPlugin._emit.
        pass


def is_audit_enabled() -> bool:
    """True when an AuditPlugin sink is registered. Lets callers skip
    payload-construction work entirely when audit is off."""
    return _GLOBAL_SINK is not None


def emit_permission_decision(
    *,
    tool_name: str,
    args: dict[str, Any],
    behavior: str,
    reason: str,
    matched_rule: Optional[dict[str, Any]],
    mode: str,
    ctx: Optional[ToolContext] = None,
) -> None:
    """Convenience emitter for the `permission_decision` event."""
    if not is_audit_enabled():
        return
    event: dict[str, Any] = {
        "ts": time.time(),
        "event": "permission_decision",
        "tool_name": tool_name,
        "tool_args": args,
        "behavior": behavior,
        "reason": reason,
        "matched_rule": matched_rule,
        "mode": mode,
    }
    if ctx is not None:
        event.update(AuditPlugin._ctx_fields(ctx))
    emit_audit_event(event)


def emit_state_mutation(
    *,
    mutation_type: str,
    state_key: str,
    details: dict[str, Any],
    ctx: Optional[ToolContext] = None,
) -> None:
    """Convenience emitter for the `state_mutation` event.

    `mutation_type` is one of: `permission_mode_change`,
    `allow_rule_added`. `details` carries type-specific fields
    (previous_value / new_value, or rule_contents / persist_across_sessions).
    """
    if not is_audit_enabled():
        return
    event: dict[str, Any] = {
        "ts": time.time(),
        "event": "state_mutation",
        "mutation_type": mutation_type,
        "state_key": state_key,
        **details,
    }
    if ctx is not None:
        event.update(AuditPlugin._ctx_fields(ctx))
    emit_audit_event(event)


def emit_confirmation_resume(
    *,
    tool_name: str,
    chose_id: Optional[str],
    confirmed: Optional[bool],
    function_call_id: Optional[str] = None,
    ctx: Optional[ToolContext] = None,
) -> None:
    """Convenience emitter for the `confirmation_resume` event —
    records when a user response lands on a gated tool call."""
    if not is_audit_enabled():
        return
    event: dict[str, Any] = {
        "ts": time.time(),
        "event": "confirmation_resume",
        "tool_name": tool_name,
        "chose_id": chose_id,
        "confirmed": confirmed,
    }
    if function_call_id is not None:
        event["function_call_id"] = function_call_id
    if ctx is not None:
        event.update(AuditPlugin._ctx_fields(ctx))
    emit_audit_event(event)


def emit_compaction_event(event_type: str, **fields: Any) -> None:
    """Convenience emitter for the three compaction event types:

      - `compaction_triggered` — before the summarizer is called.
        Typical fields: `event_count`, `last_event_ts`, `model_id`.
      - `compaction_success`   — after a non-None return.
        Typical fields: `event_count`, `summary_bytes`, `elapsed_ms`.
      - `compaction_failure`   — None return / exception / timeout.
        Typical fields: `reason` (`"empty_summary"` / `"exception"` /
        `"timeout"`), `error_type`, `error_message`, `elapsed_ms`.

    Caller is responsible for choosing a stable `event_type` from the
    three above. Any extra kwargs become top-level fields on the
    emitted JSONL object.

    No-op when no AuditPlugin sink is registered."""
    if not is_audit_enabled():
        return
    event: dict[str, Any] = {
        "ts": time.time(),
        "event": event_type,
        **fields,
    }
    emit_audit_event(event)
