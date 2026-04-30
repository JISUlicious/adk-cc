"""Exit plan mode by mutating session state.

Mirrors upstream's `ExitPlanModeV2Tool`. The tool itself is read-only
(no FS writes) — it just flips the `permission_mode` state key from
"plan" back to "default". The model invokes it after presenting its
plan and the user (implicitly, by not interrupting) accepts.

The PermissionPlugin's plan-mode block in the engine and the
PlanModeReminderPlugin's system-reminder both consult the same state
key, so flipping it here removes both gates on the next iteration.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, Field

from .base import AdkCcTool, ToolMeta


class ExitPlanModeArgs(BaseModel):
    plan_summary: str = Field(
        description=(
            "A short summary of the plan you've prepared. Visible to the "
            "user as the rationale for resuming execution."
        )
    )


class ExitPlanModeTool(AdkCcTool):
    meta = ToolMeta(
        name="exit_plan_mode",
        is_read_only=True,
        is_concurrency_safe=False,
    )
    input_model = ExitPlanModeArgs
    description = (
        "Exit plan mode and resume normal execution. Call this when you "
        "have a plan ready and are about to start acting on it. Provide "
        "a short plan_summary so the user sees what you're committing to."
    )

    async def _execute(
        self, args: ExitPlanModeArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        try:
            previous = ctx.state.get("permission_mode")
        except Exception:
            previous = None
        try:
            ctx.state["permission_mode"] = "default"
        except Exception as e:
            return {"status": "error", "error": f"could not update state: {e}"}
        return {
            "status": "ok",
            "previous_mode": previous,
            "new_mode": "default",
            "plan_summary": args.plan_summary,
        }
