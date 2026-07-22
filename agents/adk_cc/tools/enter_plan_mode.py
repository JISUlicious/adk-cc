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

import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, Field

from .base import AdkCcTool, ToolMeta

# NOTE: `..plugins.audit.emit_state_mutation` is imported lazily inside
# `_execute` rather than at module-load — tools/__init__.py is loaded
# transitively via permissions/engine.py's `from ..tools.base import
# AdkCcTool`, so an eager import here would trigger plugins/__init__.py
# while permissions/engine.py is still loading → circular import.

_log = logging.getLogger(__name__)


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
        "Switch the session into plan mode. Your tool surface narrows to "
        "read tools plus `write_plan`, `read_current_plan`, "
        "`ask_user_question`, the `Explore` sub-agent, and `exit_plan_mode`. "
        "Write/exec tools are filtered out. Use when the work needs a "
        "written plan with user approval before any change is made; the "
        "user approves via `exit_plan_mode` when you're ready to act."
    )

    def __init__(self, *, default_mode: str = "default") -> None:
        super().__init__()
        # Mirror of ExitPlanModeTool: fall back to env-set default when
        # state["permission_mode"] is unset, so the noop guard correctly
        # identifies "already in plan mode" on fresh sessions that boot
        # with ADK_CC_PERMISSION_MODE=plan. Without this, the model
        # would see "ok, switched to plan" when it was already there
        # by env default — confusing but not catastrophic. Symmetric to
        # the exit tool fix.
        self._default_mode = (default_mode or "default").lower()

    async def _execute(
        self, args: EnterPlanModeArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        try:
            previous = ctx.state.get("permission_mode")
        except Exception:
            previous = None
        if not previous:
            previous = self._default_mode
        if previous == "plan":
            return {
                "status": "noop",
                "current_mode": "plan",
                "message": (
                    "Already in plan mode. Use `write_plan` to persist the "
                    "plan and `exit_plan_mode` when ready to execute."
                ),
            }
        try:
            ctx.state["permission_mode"] = "plan"
            # F4 (dogfooding): remember the ORIGINAL posture so exit_plan_mode
            # can restore it — a desktop bypassPermissions session must come
            # back as bypassPermissions, not hardcoded "default" (which turned
            # every post-approval write into a confirmation prompt).
            ctx.state["plan_previous_mode"] = previous
        except Exception as e:
            return {"status": "error", "error": f"could not update state: {e}"}
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug(
                "state_mutation permission_mode %s -> plan (reason=%s)",
                previous,
                args.reason,
                extra={
                    "mutation_type": "permission_mode_change",
                    "state_key": "permission_mode",
                    "previous_value": previous,
                    "new_value": "plan",
                    "reason": args.reason,
                },
            )
        from ..plugins.audit import emit_state_mutation  # deferred — see module-top NOTE
        emit_state_mutation(
            mutation_type="permission_mode_change",
            state_key="permission_mode",
            details={
                "previous_value": previous,
                "new_value": "plan",
                "reason": args.reason,
            },
            ctx=ctx,
        )
        return {
            "status": "ok",
            "previous_mode": previous,
            "new_mode": "plan",
            "reason": args.reason,
        }
