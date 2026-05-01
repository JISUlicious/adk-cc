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
        # Defensive idempotency — exiting from non-plan mode is meaningless;
        # don't pretend it happened.
        if previous != "plan":
            return {
                "status": "noop",
                "current_mode": previous or "default",
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
