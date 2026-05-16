"""Pieces shared across every specialist's `agent.py`.

Two shared concerns:

  - The LLM binding (LiteLlm against a local OpenAI-compatible
    endpoint). Same model for every specialist — overridable via
    `ADK_CC_MODEL` / `ADK_CC_API_BASE` / `ADK_CC_API_KEY`.
  - The post-specialist handback callback that keeps the parent
    coordinator's flow loop alive after a specialist returns. See
    `_force_coordinator_continuation` for the why.

Keeping these here means each specialist's `agent.py` is short and
focused on its own tool surface + prompt — not on the boilerplate
of "how do I bind to the model and hand control back."
"""

from __future__ import annotations

import os

from google.adk.agents.context import Context
from google.adk.models.lite_llm import LiteLlm
from google.genai import types


def make_specialist_model() -> LiteLlm:
    """Build the LiteLlm instance used by every specialist.

    A specialist may override its `model` field after construction
    (e.g. a test injecting a scripted LLM) — this just supplies the
    production default.
    """
    return LiteLlm(
        model=os.environ.get("ADK_CC_MODEL", "openai/Qwen3.6-35B-A3B-UD-MLX-4bit"),
        api_base=os.environ.get("ADK_CC_API_BASE", "http://localhost:18000/v1"),
        api_key=os.environ["ADK_CC_API_KEY"],
    )


def make_critic_model() -> LiteLlm:
    """Build the LiteLlm instance for the `critic` sub-agent.

    The critic is intentionally model-independent from the rest of
    the pipeline. Same-model-as-coordinator grading has limited
    independence (shared priors, shared blind spots), so operators
    can wire a different model here via the `ADK_CC_CRITIC_*` env
    triplet. When those are unset, falls back to the main agent's
    config — same model, but the critic's invocation context is
    fresh so there's still SOME independence.
    """
    return LiteLlm(
        model=os.environ.get(
            "ADK_CC_CRITIC_MODEL",
            os.environ.get("ADK_CC_MODEL", "openai/Qwen3.6-35B-A3B-UD-MLX-4bit"),
        ),
        api_base=os.environ.get(
            "ADK_CC_CRITIC_API_BASE",
            os.environ.get("ADK_CC_API_BASE", "http://localhost:18000/v1"),
        ),
        api_key=os.environ.get("ADK_CC_CRITIC_API_KEY")
        or os.environ["ADK_CC_API_KEY"],
    )


def force_coordinator_continuation(callback_context: Context) -> types.Content:
    """Yield a synthetic function-call event so the parent flow doesn't
    treat the specialist's final text as the turn's final response —
    keeps `base_llm_flow.run_async`'s while-loop alive for one more
    coordinator LLM call.

    The function-call name is never executed; it's a control signal,
    not a real tool dispatch.
    """
    return types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(
                    name="_handback_to_coordinator",
                    args={},
                )
            )
        ],
    )
