"""Exit plan mode after explicit user confirmation.

Mirrors upstream's `ExitPlanModeV2Tool`. The tool gates on
`tool_context.tool_confirmation` — the standard ADK HITL pattern (see
`google/adk/tools/bash_tool.py:163-174` for the canonical example):

  1. First call: `tool_confirmation` is None → call
     `tool_context.request_confirmation(...)` to surface the plan to the
     user via the frontend, set `actions.skip_summarization = True`,
     and return an "awaiting" payload. ADK pauses.
  2. User approves or rejects via the frontend.
  3. Tool is re-invoked with `tool_confirmation` populated:
       - confirmed → flip `state["permission_mode"] = "default"` and
         return success.
       - rejected → leave plan mode active and return cancelled.

The PermissionPlugin's plan-mode block in the engine and the
PlanModeReminderPlugin's system-reminder both consult the same state
key, so the flip removes both gates on the next iteration.
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
        # Marks the call as long-running so the runtime understands it
        # may pause for user confirmation.
        long_running=True,
    )
    input_model = ExitPlanModeArgs
    description = (
        "Request approval to exit plan mode. The user is shown your "
        "plan_summary and must explicitly approve before write tools "
        "become available again. Call this when you have a plan ready "
        "to execute."
    )

    async def _execute(
        self, args: ExitPlanModeArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        confirmation = getattr(ctx, "tool_confirmation", None)

        if confirmation is None:
            # First invocation — request user approval. ADK pauses the
            # call until the frontend submits a confirmation.
            try:
                ctx.request_confirmation(
                    hint=f"Approve plan and exit plan mode?\n\n{args.plan_summary}",
                    payload={"plan_summary": args.plan_summary},
                )
                ctx.actions.skip_summarization = True
            except Exception as e:
                # No function_call_id (e.g. unit-test contexts) — degrade
                # to an error rather than silently flipping mode.
                return {
                    "status": "error",
                    "error": f"could not request confirmation: {e}",
                }
            return {
                "status": "awaiting_user_confirmation",
                "plan_summary": args.plan_summary,
            }

        # Re-entry after user response.
        if not getattr(confirmation, "confirmed", False):
            return {
                "status": "denied",
                "mode_unchanged": True,
                "plan_summary": args.plan_summary,
            }

        # User approved — flip the mode.
        try:
            previous = ctx.state.get("permission_mode")
        except Exception:
            previous = None
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
