"""Gather / act / verify agent loop on Google ADK 1.31.1.

Mirrors Claude Code's pattern from src/tools/AgentTool/built-in/:
  - One coordinator (the "main agent") owns user I/O.
  - Three specialists (Explore, Plan, verification) wired as `sub_agents`.
    Delegation is an LLM-driven `transfer_to_agent` call — and because
    sub-agents share the parent's invocation context, all their tool
    calls and responses stream into the parent event log (visible in
    `adk web`), not buried inside an opaque tool result.

Forcing "coordinator owns user I/O" requires TWO mechanisms — neither
alone is enough:

  1. `disallow_transfer_to_parent=True` on each specialist. ADK's
     runner._find_agent_to_run walks events backward to pick whose turn
     it is and only accepts an agent for which
     _is_transferable_across_agent_tree() is True — which requires
     disallow_transfer_to_parent=False on the agent and all ancestors.
     Setting it to True on each specialist makes the runner skip them
     and fall back to the root (coordinator). This is the HARD guarantee
     that the next user message always lands on the coordinator.

  2. An after_agent_callback that yields a non-final-response event when
     the specialist finishes. base_llm_flow.run_async loops until
     last_event.is_final_response() returns True. A text-only message is
     final; a Content with a function_call Part is NOT (see Event.is_
     final_response in events/event.py). Yielding a synthetic function
     call as the specialist's last event keeps the parent's flow in its
     while-loop, which triggers another coordinator LLM step. The
     coordinator then sees the specialist's report in history and
     produces the user-facing reply.

  - `disallow_transfer_to_peers=True` blocks specialist→specialist hops.
  - Tool denylists stay structural: read-only specialists simply don't
    receive write tools.
  - The verifier's discipline stays prompt-enforced + parsed: it must end
    with `VERDICT: PASS|FAIL|PARTIAL`, which the coordinator's prompt
    tells it to look for.

Discovered by `adk web` / `adk run` via the module-level `root_agent`.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

from . import prompts, tools


def _force_coordinator_continuation(callback_context: Context) -> types.Content:
    """Force the parent flow to take another step after a specialist finishes.

    Returning a Content with a function_call Part makes the wrapping Event
    fail Event.is_final_response(), which keeps base_llm_flow.run_async in
    its while-True loop and triggers another coordinator LLM call. The
    coordinator then synthesizes the user-facing reply from the
    specialist's output in the conversation history.

    The synthetic call is for a no-op name, never executed; it's a control
    signal, not a real tool call.
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

# Local model via LiteLLM, talking to an OpenAI-compatible server (mlx_lm /
# vLLM / llama.cpp / LM Studio). Defaults target localhost:18000 serving
# Qwen3.6-35B-A3B-UD-MLX-4bit. ADK_CC_API_KEY is loaded by `adk web` /
# `adk run` from .env in the agent directory. Override any of:
#   ADK_CC_MODEL=openai/<model-id>
#   ADK_CC_API_BASE=http://host:port/v1
#   ADK_CC_API_KEY=<token>
MODEL = LiteLlm(
    model=os.environ.get("ADK_CC_MODEL", "openai/Qwen3.6-35B-A3B-UD-MLX-4bit"),
    api_base=os.environ.get("ADK_CC_API_BASE", "http://localhost:18000/v1"),
    api_key=os.environ["ADK_CC_API_KEY"],
)


# ---------- specialist agents (read-only) ----------

explore_agent = LlmAgent(
    name="Explore",
    model=MODEL,
    description=(
        "Fast read-only codebase explorer. Use for broad searches across files "
        "or when a question will take more than ~3 directed queries to answer. "
        "Returns a written report; does not modify files."
    ),
    instruction=prompts.EXPLORE_INSTRUCTION,
    tools=[tools.read_file, tools.glob_files, tools.grep],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=_force_coordinator_continuation,
)

plan_agent = LlmAgent(
    name="Plan",
    model=MODEL,
    description=(
        "Read-only software architect. Explores the codebase and returns a "
        "step-by-step implementation plan plus a list of critical files. "
        "Use when designing the approach for a non-trivial change."
    ),
    instruction=prompts.PLAN_INSTRUCTION,
    tools=[tools.read_file, tools.glob_files, tools.grep],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=_force_coordinator_continuation,
)

verify_agent = LlmAgent(
    name="verification",
    model=MODEL,
    description=(
        "Adversarial verifier. Runs builds, tests, linters, and adversarial "
        "probes against changes. Cannot modify the project (writes to /tmp "
        "only via run_bash). Always ends with a literal "
        "'VERDICT: PASS|FAIL|PARTIAL' line. Invoke after non-trivial "
        "implementation (3+ file edits, backend/API, or infra changes)."
    ),
    instruction=prompts.VERIFY_INSTRUCTION,
    tools=[tools.read_file, tools.glob_files, tools.grep, tools.run_bash],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=_force_coordinator_continuation,
)


# ---------- coordinator (the "main agent") ----------

root_agent = LlmAgent(
    name="coordinator",
    model=MODEL,
    description="Coordinator agent: handles user requests with a gather → act → verify loop.",
    instruction=prompts.COORDINATOR_INSTRUCTION,
    tools=[
        tools.read_file,
        tools.glob_files,
        tools.grep,
        tools.write_file,
        tools.edit_file,
        tools.run_bash,
    ],
    sub_agents=[explore_agent, plan_agent, verify_agent],
)
