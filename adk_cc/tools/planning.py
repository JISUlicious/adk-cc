"""PLAN-stage tools: record and inspect the agent's analysis plan.

The plan lives in `tool_context.state["temp:loop_plan"]` as a list of
`{step: str, status: 'pending'|'done', evidence: Optional[str]}` dicts.
StageGuardPlugin reads this to (a) advance the loop stage to "act"
once a plan has been recorded, and (b) gate `verify_completion` so it
can't run before any plan exists.
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

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
            "'Aggregate revenue by region for sales_q1')."
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
        "data and BEFORE invoking any acting tool. Replaces any prior "
        "plan in this session."
    )

    async def _execute(self, args: _RecordPlanArgs, ctx: Any) -> dict[str, Any]:
        plan = [
            {"step": step.strip(), "status": "pending", "evidence": None}
            for step in args.steps
            if step.strip()
        ]
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
        "Return the current plan (steps + status). Call this whenever "
        "you want to remind yourself what's pending / done."
    )

    async def _execute(self, args: _NoArgs, ctx: Any) -> dict[str, Any]:
        plan = ctx.state.get(_PLAN_KEY) or []
        return {"status": "ok", "plan": plan, "exists": bool(plan)}


# --- mark_step_done ------------------------------------------------


class _MarkArgs(BaseModel):
    step_index: int = Field(..., ge=0, description="0-based plan step index.")
    evidence: Optional[str] = Field(
        None,
        description=(
            "Short string capturing what this step produced — a number, "
            "tool name, or summary. Used by `verify_completion` later."
        ),
    )


class MarkStepDoneTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="mark_step_done",
        is_read_only=False,
        is_concurrency_safe=False,
    )
    input_model: ClassVar[type[BaseModel]] = _MarkArgs
    description: ClassVar[str] = (
        "Flip a plan step to status=done with optional evidence string. "
        "ACT-stage bookkeeping: call this after each acting tool returns "
        "the value the step asked for."
    )

    async def _execute(self, args: _MarkArgs, ctx: Any) -> dict[str, Any]:
        plan = ctx.state.get(_PLAN_KEY) or []
        if args.step_index >= len(plan):
            return {
                "status": "error",
                "error": f"step_index {args.step_index} out of range (plan has {len(plan)} steps)",
            }
        plan[args.step_index] = {
            **plan[args.step_index],
            "status": "done",
            "evidence": args.evidence,
        }
        ctx.state[_PLAN_KEY] = plan
        remaining = sum(1 for p in plan if p["status"] != "done")
        return {
            "status": "ok",
            "step_index": args.step_index,
            "remaining": remaining,
            "all_done": remaining == 0,
        }
