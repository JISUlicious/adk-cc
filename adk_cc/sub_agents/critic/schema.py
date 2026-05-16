"""Structured verdict the `critic` sub-agent emits.

This Pydantic model is BOTH the critic's `output_schema` (forces the
model to produce JSON matching this shape) AND the type the
`verify_completion` tool accepts as its `critic_verdict` arg. Two
consumers, one canonical definition — avoids the shape drifting
between producer and consumer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CriticVerdict(BaseModel):
    """Independent judgment of whether the coordinator's conclusion
    actually answers the user's query. Adversarial framing: the
    critic's job is to find what's MISSING, not to confirm what's
    present.
    """

    verdict: Literal["PASS", "FAIL", "PARTIAL"] = Field(
        ...,
        description=(
            "Top-line judgment. PASS only when the conclusion fully and "
            "correctly answers the user's query. FAIL when something "
            "substantive is wrong or missing. PARTIAL when the answer "
            "is correct for what it addresses but leaves named parts of "
            "the query unanswered."
        ),
    )
    addressed_aspects: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete parts of the user's query that the conclusion "
            "actually answers, each as a short phrase. Empty if the "
            "conclusion addresses nothing the query asked for."
        ),
    )
    missing_aspects: list[str] = Field(
        default_factory=list,
        description=(
            "Parts of the user's query the conclusion does NOT answer, "
            "each as a short phrase. Empty when verdict=PASS. The "
            "coordinator should re-dispatch work to address these "
            "before re-criticizing."
        ),
    )
    evidence_quality: Literal["strong", "weak", "insufficient"] = Field(
        ...,
        description=(
            "How well the recorded tool results back up the conclusion. "
            "strong = numbers cited match acting-tool outputs; "
            "weak = conclusion goes beyond what the tools demonstrated; "
            "insufficient = not enough tool results to support the claim."
        ),
    )
    reasoning: str = Field(
        ...,
        min_length=10,
        description=(
            "2-3 sentence narrative explaining the verdict. Should "
            "name SPECIFIC facts from the session — which tool returned "
            "what, which part of the query is addressed by which result. "
            "Generic prose like 'looks good' is insufficient."
        ),
    )
