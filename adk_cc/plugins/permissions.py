"""Permission plugin — the integration point with ADK's plugin chain.

Registered on `Runner(plugins=[...])`. Runs `before_tool_callback` for
every tool call across every agent.

Behavior:
  - For non-AdkCcTool tools (e.g. ADK built-ins, MCP tools without a
    ToolMeta), the plugin passes through. Tighten this by listing
    expected tool classes if you want a default-deny posture.
  - For AdkCcTool subclasses, the plugin reads the active mode from
    `tool_context.state["permission_mode"]` (default DEFAULT) and runs
    the engine.
  - On `deny`, the plugin returns a structured dict that short-circuits
    the tool execution; the dict surfaces back to the LLM so it sees
    the denial and can adjust.
  - On `ask`, the plugin (a) calls `tool_context.request_confirmation()`
    when a `function_call_id` is available — letting the runtime pause
    the call for HITL — and (b) returns a structured dict so the model
    is informed even when no HITL UI is attached. Stage E will refine
    this into a proper resume flow.
  - On `allow`, the plugin returns None and the tool runs normally.
"""

from __future__ import annotations

from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..permissions.engine import decide
from ..permissions.modes import PermissionMode
from ..permissions.settings import SettingsHierarchy
from ..tools.base import AdkCcTool


class PermissionPlugin(BasePlugin):
    def __init__(
        self,
        settings: SettingsHierarchy,
        *,
        default_mode: PermissionMode = PermissionMode.DEFAULT,
        name: str = "adk_cc_permissions",
    ) -> None:
        super().__init__(name=name)
        self._settings = settings
        self._default_mode = default_mode

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        if not isinstance(tool, AdkCcTool):
            return None

        mode = self._mode_from_context(tool_context)
        decision = decide(
            tool=tool, args=tool_args, mode=mode, settings=self._settings
        )

        if decision.behavior == "deny":
            return {
                "status": "permission_denied",
                "reason": decision.reason,
                "matched_rule": (
                    decision.matched_rule.model_dump()
                    if decision.matched_rule
                    else None
                ),
            }

        if decision.behavior == "ask":
            # Surface a HITL pause when the runtime supports it. Tool calls
            # without a function_call_id (rare, but possible in some test
            # contexts) skip this without erroring.
            if tool_context.function_call_id:
                tool_context.request_confirmation(hint=decision.reason)
                # CRITICAL: ADK's loop breaks when the last yielded event's
                # `is_final_response()` is True, which requires either
                # `actions.skip_summarization` or `long_running_tool_ids` to
                # be set. Setting `requested_tool_confirmations` alone is NOT
                # enough — ADK yields a separate request-confirmation event
                # (which IS final), but then yields the function_response_event
                # AFTER, and the loop checks the last yielded event. Without
                # this flag, the runner re-invokes the LLM before the user has
                # confirmed, the model sees `{"status": "needs_confirmation"}`
                # as a normal tool result, and decides to call another tool —
                # cascading confirmations queue up. AdkCcTool.run_async sets
                # this for the same reason; PermissionPlugin must too.
                tool_context.actions.skip_summarization = True
            return {
                "status": "needs_confirmation",
                "reason": decision.reason,
                "matched_rule": (
                    decision.matched_rule.model_dump()
                    if decision.matched_rule
                    else None
                ),
            }

        return None

    def _mode_from_context(self, ctx: ToolContext) -> PermissionMode:
        try:
            raw = ctx.state.get("permission_mode")
        except Exception:
            raw = None
        if not raw:
            return self._default_mode
        try:
            return PermissionMode(raw)
        except ValueError:
            return self._default_mode
