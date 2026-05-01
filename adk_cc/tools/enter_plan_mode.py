"""Enter plan mode mid-session, gated on user approval.

Symmetric to `ExitPlanModeTool`. Useful when the model recognizes a
request that warrants careful planning (architecture changes, security-
sensitive work, multi-step refactors) and wants to switch into plan-mode
posture for the rest of the session — or when the user wants to escalate
into planning without restarting.

Same approval flow as ExitPlanMode (`requires_user_approval=True` →
`AdkCcTool.run_async` runs the request_confirmation dance). The model's
`reason` is shown to the user in the confirmation dialog so they know
why entering plan mode is being proposed.

Plan-mode posture takes effect on the NEXT iteration: the engine's
Step 2a starts blocking writes, the reminder plugin starts injecting,
and the visibility plugin starts hiding `exit_plan_mode` to surface
`enter_plan_mode` instead.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, Field

from .base import AdkCcTool, ToolMeta


class EnterPlanModeArgs(BaseModel):
    reason: str = Field(
        description=(
            "Why plan mode is appropriate here. Shown to the user in the "
            "confirmation dialog so they know what they're approving."
        )
    )


class EnterPlanModeTool(AdkCcTool):
    meta = ToolMeta(
        name="enter_plan_mode",
        is_read_only=True,
        is_concurrency_safe=False,
        requires_user_approval=True,
    )
    input_model = EnterPlanModeArgs
    description = (
        "Request approval to enter plan mode. While in plan mode, write "
        "and execute tools are blocked; only read tools and the Plan / "
        "Explore sub-agents remain available. Use when the next steps "
        "need careful design before any change is made."
    )

    def _approval_hint(self, args: EnterPlanModeArgs) -> str:
        return f"Enter plan mode?\n\nReason: {args.reason}"

    def _approval_payload(self, args: EnterPlanModeArgs) -> dict[str, str]:
        return {"reason": args.reason}

    async def _execute(
        self, args: EnterPlanModeArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        try:
            previous = ctx.state.get("permission_mode")
        except Exception:
            previous = None
        try:
            ctx.state["permission_mode"] = "plan"
        except Exception as e:
            return {"status": "error", "error": f"could not update state: {e}"}
        return {
            "status": "approved",
            "previous_mode": previous,
            "new_mode": "plan",
            "reason": args.reason,
        }
