"""VERIFY-stage tool: rule-checks + an independent critic's verdict.

`verify_completion` is the durable PASS/FAIL gate. It combines two
sources of evidence:

  - RULE side (deterministic, fast): plan was recorded, result count
    >= plan length, conclusion is non-empty. Catches obvious skips.
  - CRITIC side (LLM, independent context): the `critic` sub-agent's
    structured verdict, passed in by the coordinator as the
    `critic_verdict` arg. Catches semantic gaps the rule check
    can't see (missing aspects of the query, weak evidence, etc.).

Final verdict is PASS only when BOTH agree. The breakdown is
returned so the caller can see which side rejected — when only one
fails, the coordinator can re-dispatch work to address it and
re-critic without restarting the whole loop.

The coordinator's earlier `llm_judgment` self-grading is GONE. A
model grading its own work in the same context has near-zero
independence; the critic sub-agent is the replacement.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator

from ..sub_agents.critic.schema import CriticVerdict
from .base import AdkCcTool, ToolMeta

_PLAN_KEY = "temp:loop_plan"
_RESULTS_KEY = "temp:loop_results"


class _VerifyArgs(BaseModel):
    user_query: str = Field(..., min_length=1)
    conclusion: str = Field(..., min_length=1)
    critic_verdict: CriticVerdict = Field(
        ...,
        description=(
            "The `critic` sub-agent's structured verdict from its most "
            "recent invocation in this session. Pass the JSON object "
            "verbatim — do not paraphrase it or invent fields. The "
            "coordinator should obtain this by dispatching to `critic` "
            "and reading the critic's structured output from the "
            "conversation history."
        ),
    )

    @field_validator("critic_verdict", mode="before")
    @classmethod
    def _accept_stringified_verdict(cls, v: Any) -> Any:
        """Some smaller / OS function-callers emit nested-object args as
        JSON-encoded strings instead of real nested objects (we hit
        this on stepfun-ai/step-3.5-flash and minimax-m2.7). Parse the
        string here so Pydantic sees the right shape downstream;
        validation failures fall through to surface the real schema
        error to the model."""
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
        "Final gate. Combines deterministic rule-checks (plan complete, "
        "results present, conclusion non-empty) with an INDEPENDENT "
        "critic's structured verdict to decide PASS / FAIL. Call this "
        "AFTER dispatching to the `critic` sub-agent and reading its "
        "verdict from conversation history.\n\n"
        "IMPORTANT: `critic_verdict` is a nested object with five "
        "fields (verdict, addressed_aspects, missing_aspects, "
        "evidence_quality, reasoning). Pass it as a real JSON object, "
        'NOT as a string: `critic_verdict: {"verdict": "PASS", ...}`. '
        "Use the critic's verdict verbatim — do not invent values."
    )

    async def _execute(self, args: _VerifyArgs, ctx: Any) -> dict[str, Any]:
        plan = ctx.state.get(_PLAN_KEY) or []
        results = ctx.state.get(_RESULTS_KEY) or []

        # --- Rule check (deterministic) ---
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

        # --- Critic check (independent LLM judgment) ---
        critic_pass = args.critic_verdict.verdict == "PASS"

        verdict = "PASS" if (rule_pass and critic_pass) else "FAIL"

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
            "critic_check": {
                "pass": critic_pass,
                "verdict": args.critic_verdict.verdict,
                "missing_aspects": args.critic_verdict.missing_aspects,
                "evidence_quality": args.critic_verdict.evidence_quality,
                "reasoning": args.critic_verdict.reasoning,
            },
        }
