"""`critic` sub-agent — adversarial verifier of the coordinator's draft.

Independent context: the critic's invocation starts fresh, reads the
user's original message + every tool call from session history, and
emits a structured `CriticVerdict` via ADK's `output_schema`
enforcement. No tools — pure judgment.

Model is configured via the `ADK_CC_CRITIC_*` env triplet (model,
api_base, api_key). Operators are encouraged to wire a DIFFERENT
model here than the main agent — heterogeneity is the main lever
for independence. Defaults to the main model if unset.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from .._shared import force_coordinator_continuation, make_critic_model
from .prompts import CRITIC_INSTRUCTION
from .schema import CriticVerdict


critic_agent = LlmAgent(
    name="critic",
    model=make_critic_model(),
    description=(
        "Adversarial verifier. Reads the user's original query + every "
        "tool result from session history; emits a structured "
        "CriticVerdict (verdict, addressed_aspects, missing_aspects, "
        "evidence_quality, reasoning). No tools — pure independent "
        "judgment. Coordinator MUST dispatch here before calling "
        "verify_completion."
    ),
    instruction=CRITIC_INSTRUCTION,
    # output_schema enforces the JSON shape downstream. Tools are
    # mutually exclusive with output_schema in ADK; the critic
    # doesn't need any (it reads everything from session events).
    output_schema=CriticVerdict,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=force_coordinator_continuation,
)
