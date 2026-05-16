"""Audit plugin — observes every tool call.

Routes through ADK's existing plugin surface directly:
  - `before_tool_callback`  → records the attempt
  - `after_tool_callback`   → records the result (only fires on executed)
  - `on_tool_error_callback`→ records errors

All callbacks return None so audit observes without mutating the chain.

Plugin chain ordering matters: register `AuditPlugin` FIRST so its
`before_tool_callback` fires before any other plugin can short-circuit.

Sink is pluggable: either a path (JSONL) or a callable. Operators wanting
Postgres / DataDog / SQS write a callable that takes the event dict.

## Event schema (versioned by `event` field)

  - `tool_call_attempt`        — every dispatched tool call
  - `tool_call_result`         — after execution
  - `tool_call_error`          — execution raised
  - `model_request`            — full LlmRequest dump (ModelIOTracePlugin)
  - `model_response`           — full LlmResponse dump (ModelIOTracePlugin)
  - `loop_stage_transition`    — stage advance via StageGuardPlugin

Events beyond the first three are emitted from inside other plugins via
the module-level `emit_audit_event` helper. The helper looks up the
process-wide AuditPlugin instance set at agent construction (see
`set_global_sink`). Callsites that fire when no AuditPlugin is
configured are silent no-ops.
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

# Process-wide sink registered by the AuditPlugin instance at __init__.
# When no plugin is configured, `_GLOBAL_SINK` stays None and emits are
# no-ops.
_GLOBAL_SINK: Optional[Callable[[dict[str, Any]], None]] = None

Sink = Union[str, Path, Callable[[dict], None]]
"""Where audit events go.

  - str/Path: JSONL file. Created on first write; one event per line.
  - callable(event_dict): operator-defined sink (e.g. Postgres insert).
"""


def _default_sink_path() -> Path:
    return Path(
        os.environ.get(
            "ADK_CC_AUDIT_LOG",
            os.path.join(os.path.expanduser("~"), ".adk-cc", "audit.jsonl"),
        )
    )


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
        # callsites (StageGuardPlugin transitions, ModelIOTrace dumps,
        # etc.) can route audit events here via `emit_audit_event`. Last
        # instance wins — operators registering a second AuditPlugin are
        # responsible for ordering.
        set_global_sink(self._emit)

    def _emit(self, event: dict[str, Any]) -> None:
        if self._sink_callable is not None:
            try:
                self._sink_callable(event)
            except Exception:
                # Audit must never raise — losing a record is preferable
                # to crashing the agent loop.
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
        # AdkCcTool carries a `meta` attribute; plain BaseTool does not.
        # Use duck-typing rather than an isinstance check so this avoids
        # a circular import between `plugins.audit` and `tools.base`.
        out: dict[str, Any] = {"tool_name": tool.name}
        meta = getattr(tool, "meta", None)
        dump = getattr(meta, "model_dump", None) if meta is not None else None
        if callable(dump):
            try:
                out["tool_meta"] = dump()
            except Exception:
                pass
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
        # ADK's after_tool_callback signature types `result` as `dict`,
        # but in practice tools can return strings (MCP tools, some
        # BaseTool impls). Defensive: only pull `status` when the result
        # is dict-shaped.
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


# --- Module-level helpers for non-plugin callsites --------------------
#
# Other plugins emit structured events via `emit_audit_event` rather
# than holding a reference to the AuditPlugin instance. When no plugin
# is registered, all emits are silent no-ops.


def set_global_sink(sink: Callable[[dict[str, Any]], None]) -> None:
    """Register the process-wide audit sink. Called by AuditPlugin.__init__.
    Tests can call directly to install a mock without instantiating the
    plugin."""
    global _GLOBAL_SINK
    _GLOBAL_SINK = sink


def clear_global_sink() -> None:
    """Test helper — drop the registered sink."""
    global _GLOBAL_SINK
    _GLOBAL_SINK = None


def emit_audit_event(event: dict[str, Any]) -> None:
    """Send a structured event to the registered audit sink. No-op when
    no AuditPlugin has been constructed in this process."""
    sink = _GLOBAL_SINK
    if sink is None:
        return
    try:
        sink(event)
    except Exception:
        # Audit must never raise — losing a record is preferable to
        # crashing the agent loop.
        pass


def is_audit_enabled() -> bool:
    """True when an AuditPlugin sink is registered."""
    return _GLOBAL_SINK is not None
