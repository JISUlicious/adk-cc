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
from google.adk.apps.app import App
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

from . import prompts
from .permissions import PermissionMode, SettingsHierarchy
from .plugins import AuditPlugin, PermissionPlugin
from .tools import (
    AskUserQuestionTool,
    BashTool,
    EditFileTool,
    GlobFilesTool,
    GrepTool,
    ReadFileTool,
    WebFetchTool,
    WriteFileTool,
    make_skill_toolset,
)


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


# ---------- shared tool instances ----------
# Tools are stateless; one instance per tool, reused across agents.
_read_file = ReadFileTool()
_glob_files = GlobFilesTool()
_grep = GrepTool()
_write_file = WriteFileTool()
_edit_file = EditFileTool()
_run_bash = BashTool()
_web_fetch = WebFetchTool()
_ask_user = AskUserQuestionTool()
_skills = make_skill_toolset()  # None unless ADK_CC_SKILLS_DIR / skills/ has content


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
    tools=[_read_file, _glob_files, _grep, _web_fetch],
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
    tools=[_read_file, _glob_files, _grep, _web_fetch],
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
    tools=[_read_file, _glob_files, _grep, _run_bash, _web_fetch],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=_force_coordinator_continuation,
)


# ---------- coordinator (the "main agent") ----------

_coordinator_tools: list = [
    _read_file,
    _glob_files,
    _grep,
    _write_file,
    _edit_file,
    _run_bash,
    _web_fetch,
    _ask_user,
]
if _skills is not None:
    _coordinator_tools.append(_skills)

root_agent = LlmAgent(
    name="coordinator",
    model=MODEL,
    description="Coordinator agent: handles user requests with a gather → act → verify loop.",
    instruction=prompts.COORDINATOR_INSTRUCTION,
    tools=_coordinator_tools,
    sub_agents=[explore_agent, plan_agent, verify_agent],
)


# ---------- App with permission plugin ----------
# `adk web` / `adk run` look for `app` first, then `root_agent`. Exposing
# both keeps direct-test imports of `root_agent` working while letting the
# CLI wire the plugin chain automatically.
#
# Default mode is `bypassPermissions` to preserve the dev experience: the
# plugin is always loaded (so Stage D/G can layer audit/quotas on top),
# but it only enforces deny rules. Flip to `default`/`plan`/`acceptEdits`/
# `dontAsk` via env to exercise the engine.
PERMISSION_MODE = PermissionMode(
    os.environ.get("ADK_CC_PERMISSION_MODE", PermissionMode.BYPASS_PERMISSIONS.value)
)
SETTINGS = SettingsHierarchy.empty()  # rules added by operators / Stage G loader

app = App(
    name="adk_cc",
    root_agent=root_agent,
    # Order matters. Audit goes first so before_tool_callback records every
    # attempt — including ones the permission plugin denies. Permission's
    # short-circuit only stops the *chain*, but audit's row is already
    # written by then.
    plugins=[
        AuditPlugin(),
        PermissionPlugin(SETTINGS, default_mode=PERMISSION_MODE),
    ],
)
