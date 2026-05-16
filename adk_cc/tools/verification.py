"""VERIFY-stage tool: rule-checks + an LLM judgment over the agent's work.

`verify_completion` is the agent's exit gate. It takes:

  - `user_query`: the original ask, restated by the agent.
  - `conclusion`: the agent's final, user-facing answer (still inside
    the tool call — not yet emitted as text).
  - `llm_judgment`: a structured dict the agent produces from its own
    reasoning about whether the conclusion answers the query. Carrying
    this through the tool args (rather than spawning a second model
    call) keeps the verifier deterministic in tests while still
    capturing the model's own self-assessment.

The tool combines that with:

  - RULE-side checks against session state — plan exists, every step
    has status=done, at least one acting-tool result was recorded,
    conclusion is non-empty, every plan step's `evidence` field is set.
  - The LLM judgment's `satisfies_query: bool` field.

The verdict is `PASS` only when rules AND LLM agree. Either side's
failure produces `FAIL`. The breakdown is returned so the caller can
see which side rejected.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator

from .base import AdkCcTool, ToolMeta

_PLAN_KEY = "temp:loop_plan"
_RESULTS_KEY = "temp:loop_results"


class _LlmJudgment(BaseModel):
    satisfies_query: bool = Field(
        ...,
        description=(
            "Your assessment: does the conclusion actually answer the "
            "original user_query? True only if the data behind the "
            "conclusion is correct AND directly responsive."
        ),
    )
    reasoning: str = Field(
        ...,
        min_length=10,
        description=(
            "One or two sentences explaining the assessment. Used as "
            "evidence in the audit trail and shown back to the user "
            "if the verifier rejects."
        ),
    )


class _VerifyArgs(BaseModel):
    user_query: str = Field(..., min_length=1)
    conclusion: str = Field(..., min_length=1)
    llm_judgment: _LlmJudgment

    @field_validator("llm_judgment", mode="before")
    @classmethod
    def _accept_stringified_judgment(cls, v: Any) -> Any:
        """Some smaller / OS function-callers (observed on
        stepfun-ai/step-3.5-flash) emit nested-object args as a
        JSON-encoded STRING instead of a real nested object. Parse
        the string here so Pydantic sees the right shape downstream.
        Failures fall through to the standard validator and surface
        the real schema error to the model.
        """
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, ValueError):
                return v
        return v


class VerifyCompletionTool(AdkCcTool):
    stage: ClassVar[str] = "verify"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="verify_completion",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _VerifyArgs
    description: ClassVar[str] = (
        "Final gate. Combines rule-checks (plan complete, evidence "
        "recorded, results present) with your own LLM judgment to "
        "decide PASS / FAIL. Call this exactly once, AFTER every plan "
        "step is marked done and BEFORE you emit the user-facing reply.\n\n"
        "IMPORTANT: `llm_judgment` is a nested object with fields "
        "`satisfies_query` (bool) and `reasoning` (string), passed "
        "directly as args — NOT a JSON-encoded string. Emit it as a "
        'real object: `llm_judgment: {"satisfies_query": true, '
        '"reasoning": "..."}`, not `llm_judgment: "{...}"`.'
    )

    async def _execute(self, args: _VerifyArgs, ctx: Any) -> dict[str, Any]:
        plan = ctx.state.get(_PLAN_KEY) or []
        results = ctx.state.get(_RESULTS_KEY) or []

        # --- Rule checks ---
        # Step completion is INFERRED from result count vs plan length —
        # one acting-tool result per plan step is the contract. There's
        # no per-step `status` to inspect because there's no
        # `mark_step_done` tool anymore. The specialist's handback IS
        # the step-done signal; the framework counts results.
        rule_failures: list[str] = []
        if not plan:
            rule_failures.append("no plan was recorded")
        elif len(results) < len(plan):
            rule_failures.append(
                f"plan has {len(plan)} step(s) but only {len(results)} "
                f"acting-tool result(s) recorded — at least one specialist "
                f"dispatch is missing"
            )
        if not results:
            rule_failures.append("no acting-tool results recorded")
        if not args.conclusion.strip():
            rule_failures.append("conclusion is empty")

        rule_pass = not rule_failures

        # --- LLM check ---
        llm_pass = args.llm_judgment.satisfies_query
        verdict = "PASS" if (rule_pass and llm_pass) else "FAIL"

        return {
            "status": "ok",
            "verdict": verdict,
            "user_query": args.user_query,
            "conclusion": args.conclusion,
            "rule_check": {
                "pass": rule_pass,
                "failures": rule_failures,
                "plan_steps": len(plan),
                "results_recorded": len(results),
            },
            "llm_check": {
                "pass": llm_pass,
                "reasoning": args.llm_judgment.reasoning,
            },
        }
