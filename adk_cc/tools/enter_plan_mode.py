"""Enter plan mode mid-session.

Useful when the model recognizes a request that warrants careful
planning (architecture changes, security-sensitive work, multi-step
refactors) and wants to switch into plan-mode posture for the rest of
the session.

No approval gate, intentionally asymmetric with `ExitPlanModeTool`:
entering plan mode tightens the agent's posture (writes are blocked
until the user approves a plan), so requiring confirmation to be MORE
cautious is friction without a safety benefit. The model's `reason`
is recorded in the tool result so it remains visible in the trace.

Plan-mode posture takes effect on the NEXT iteration: the engine's
Step 2a starts blocking writes, the reminder plugin starts injecting,
and the visibility plugin starts hiding `enter_plan_mode` to surface
`exit_plan_mode` instead.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, Field

from .base import AdkCcTool, ToolMeta


class EnterPlanModeArgs(BaseModel):
    reason: str = Field(
        description="Why plan mode is appropriate here. Recorded in the tool result for the trace."
    )


class EnterPlanModeTool(AdkCcTool):
    meta = ToolMeta(
        name="enter_plan_mode",
        is_read_only=True,
        is_concurrency_safe=False,
        requires_user_approval=False,
    )
    input_model = EnterPlanModeArgs
    description = (
        "Switch the session into plan mode. While in plan mode, write "
        "and execute tools are blocked; only read tools and the Plan / "
        "Explore sub-agents remain available. Use when the next steps "
        "need careful design before any change is made. The user will "
        "approve the plan via `exit_plan_mode` when you're ready to act."
    )

    async def _execute(
        self, args: EnterPlanModeArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        try:
            previous = ctx.state.get("permission_mode")
        except Exception:
            previous = None
        if previous == "plan":
            return {
                "status": "noop",
                "current_mode": "plan",
                "message": (
                    "Already in plan mode. Use the Plan sub-agent or "
                    "write_plan / read_current_plan; call exit_plan_mode "
                    "when ready to execute."
                ),
            }
        try:
            ctx.state["permission_mode"] = "plan"
        except Exception as e:
            return {"status": "error", "error": f"could not update state: {e}"}
        return {
            "status": "ok",
            "previous_mode": previous,
            "new_mode": "plan",
            "reason": args.reason,
        }
