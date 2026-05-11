"""Exit plan mode after explicit user confirmation.

Mirrors upstream's `ExitPlanModeV2Tool.checkPermissions: 'ask'` pattern
via adk-cc's `ToolMeta.requires_user_approval` flag. The two-call
confirmation dance lives in `AdkCcTool.run_async`; this tool only sees
control after the user has approved, which lets `_execute` be a
straightforward state mutation.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, Field

from .base import AdkCcTool, ToolMeta


class ExitPlanModeArgs(BaseModel):
    plan_summary: str = Field(
        description=(
            "A short summary of the plan you've prepared. Surfaced to the "
            "user during confirmation so they know what they're approving."
        )
    )


class ExitPlanModeTool(AdkCcTool):
    meta = ToolMeta(
        name="exit_plan_mode",
        is_read_only=True,
        is_concurrency_safe=False,
        # The base class's run_async sees this and runs the request_confirmation
        # dance before _execute. Equivalent to upstream's `checkPermissions: 'ask'`.
        requires_user_approval=True,
    )
    input_model = ExitPlanModeArgs
    description = (
        "Request approval to exit plan mode. The user is shown your "
        "plan_summary and must explicitly approve before write tools "
        "become available again. Call this when you have a plan ready "
        "to execute."
    )

    def __init__(self, *, default_mode: str = "default") -> None:
        super().__init__()
        # Fall back to the env-set default when session state hasn't been
        # written yet. Same pattern PR #4 used for the plan-mode plugins.
        #
        # The bug it prevents: with ADK_CC_PERMISSION_MODE=plan, a fresh
        # session is in plan-mode posture via the plugin-layer fallback
        # (PR #4), but state["permission_mode"] is still None. Without
        # this fallback, `previous = state.get(...)` is None, the
        # `previous != "plan"` guard trips, the tool returns noop, and
        # state["permission_mode"] is NEVER written. The next turn the
        # plugins fall back to env-default="plan" again → stuck loop.
        self._default_mode = (default_mode or "default").lower()

    def _approval_hint(self, args: ExitPlanModeArgs) -> str:
        return f"Approve plan and exit plan mode?\n\n{args.plan_summary}"

    def _approval_payload(self, args: ExitPlanModeArgs) -> dict[str, str]:
        return {"plan_summary": args.plan_summary}

    async def _execute(
        self, args: ExitPlanModeArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        # By the time we get here, AdkCcTool.run_async has confirmed user
        # approval. Just flip the mode and return.
        try:
            previous = ctx.state.get("permission_mode")
        except Exception:
            previous = None
        # Fall back to env default so "fresh session + env=plan" is
        # correctly seen as "in plan mode" for the noop guard.
        if not previous:
            previous = self._default_mode
        # Defensive idempotency — exiting from non-plan mode is meaningless;
        # don't pretend it happened.
        if previous != "plan":
            return {
                "status": "noop",
                "current_mode": previous,
                "message": (
                    f"Not in plan mode (current: {previous!r}); nothing to exit. "
                    "Use the regular tools to proceed."
                ),
            }
        try:
            ctx.state["permission_mode"] = "default"
        except Exception as e:
            return {"status": "error", "error": f"could not update state: {e}"}
        return {
            "status": "approved",
            "previous_mode": previous,
            "new_mode": "default",
            "plan_summary": args.plan_summary,
        }
