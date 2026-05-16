"""PLAN-stage tools: record and inspect the agent's analysis plan.

The plan lives in `tool_context.state["temp:loop_plan"]` as a list of
step strings — that's it. There's no per-step `status` or `evidence`
field; step completion is INFERRED from the number of acting-tool
results recorded in `temp:loop_results`. When that count reaches the
plan length, `StageGuardPlugin` advances `act → verify` and
`verify_completion`'s rule check passes the "all steps done"
condition.

This shape exists because earlier designs had a `mark_step_done`
tool the coordinator was supposed to call between specialist
dispatches. On real-LLM runs (minimax-m2.7) specialists kept trying
to call `mark_step_done` themselves — it wasn't on their tool list,
so it tripped a "tool not found" error every time. Removing the
tool entirely removed the failure mode: now a specialist's handback
IS the step-done signal, and the framework counts results.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from .base import AdkCcTool, ToolMeta

_PLAN_KEY = "temp:loop_plan"


# --- record_plan ---------------------------------------------------


class _RecordPlanArgs(BaseModel):
    steps: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Ordered list of analysis steps. Each entry is an imperative "
            "sentence describing what you will compute next (e.g. "
            "'Aggregate revenue by region for sales_q1'). Each step "
            "should correspond to ONE specialist dispatch — when the "
            "specialist's tool returns, that step is considered done."
        ),
    )


class RecordPlanTool(AdkCcTool):
    stage: ClassVar[str] = "plan"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="record_plan",
        is_read_only=False,
        is_concurrency_safe=False,
    )
    input_model: ClassVar[type[BaseModel]] = _RecordPlanArgs
    description: ClassVar[str] = (
        "Persist the analysis plan (ordered list of steps) into session "
        "state. PLAN-stage tool: call this AFTER you have explored the "
        "data and BEFORE invoking any acting specialist. Each step in "
        "the list should correspond to ONE acting-specialist dispatch."
    )

    async def _execute(self, args: _RecordPlanArgs, ctx: Any) -> dict[str, Any]:
        plan = [step.strip() for step in args.steps if step.strip()]
        if not plan:
            return {"status": "error", "error": "no non-empty steps provided"}
        ctx.state[_PLAN_KEY] = plan
        return {"status": "ok", "steps_recorded": len(plan), "plan": plan}


# --- read_plan -----------------------------------------------------


class _NoArgs(BaseModel):
    pass


class ReadPlanTool(AdkCcTool):
    stage: ClassVar[str] = "plan"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="read_plan",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _NoArgs
    description: ClassVar[str] = (
        "Return the current plan (list of step strings). Call this "
        "whenever you want to remind yourself what's pending. Step "
        "completion is tracked by the framework, not stored on the "
        "plan itself."
    )

    async def _execute(self, args: _NoArgs, ctx: Any) -> dict[str, Any]:
        plan = ctx.state.get(_PLAN_KEY) or []
        return {"status": "ok", "plan": plan, "step_count": len(plan)}
