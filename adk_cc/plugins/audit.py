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

from ..tools.base import AdkCcTool

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
