"""Plan-mode behavioral helper plugin.

When `permission_mode == "plan"` the coordinator becomes a planning agent:
it cannot write, run shell, or mutate task state. It produces a plan via
`write_plan` and ends its turn with `exit_plan_mode`, which gates re-entry
to the unrestricted tool surface on user approval.

This plugin enforces that posture at the LLM-tool-surface layer (so the
model never even sees the tools it can't use):

  1. **Tool visibility** — when in plan mode, hide write/exec tools and
     `enter_plan_mode` (you're already in it). Keep read tools, plan
     tools (`write_plan`, `read_current_plan`), `exit_plan_mode`,
     `ask_user_question`, and `transfer_to_agent('Explore')`. When NOT
     in plan mode, hide `exit_plan_mode` (nothing to exit) but leave the
     write tools alone. Filter both `llm_request.tools_dict` and each
     `tool_obj.function_declarations` — both are read by the model layer.
  2. **Reminder injection** — append a planning instruction to the
     coordinator's `system_instruction` describing how to plan, what the
     plan file should contain, and how to exit. The LLM gets the same
     planning rigor the old standalone Plan sub-agent had, without the
     transfer ceremony.

Skipped for read-only specialists (Explore, verification): they're already
shaped for their narrow purpose and don't own the enter/exit surface.

Note on history: an earlier design routed planning through a `Plan`
sub-agent invoked via `transfer_to_agent`. That mechanism overlapped with
plan mode (both produced "plan, then act") and the redundancy caused the
model to do both — entering plan mode AND transferring. The unification
collapses planning into a single posture the coordinator takes.
"""

from __future__ import annotations

from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

# Read-only specialists are skipped entirely. They don't own the
# enter/exit surface and the reminder would just burn tokens.
_SPECIALIST_AGENTS = frozenset({"Explore", "verification"})

# Tools the LLM should NOT see while in plan mode. Hiding them at the
# tool-surface layer is stronger than relying on permission denials —
# the model can't call what it doesn't see, and `ToolCallValidatorPlugin`
# catches any hallucinated calls as a safety net.
_PLAN_MODE_HIDDEN_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "run_bash",
    "task_create",
    "task_update",
    "enter_plan_mode",
})

# Hidden when NOT in plan mode (nothing to exit).
_NORMAL_MODE_HIDDEN_TOOLS = frozenset({"exit_plan_mode"})


PLAN_MODE_REMINDER = """<system-reminder>
YOU ARE CURRENTLY IN PLAN MODE. The user has asked you to plan rather
than execute. You MUST NOT make any edits, run shell, or mutate task
state. The write tools have been removed from your tool surface; use
only the tools currently visible to you.

## Your process

1. **Understand the request**: read the user's message carefully. If
   it's ambiguous, call `ask_user_question` BEFORE planning — clarifying
   intent is cheaper than rewriting the plan. Call `read_current_plan`
   first; if a plan already exists for this session, decide whether to
   refine that thread (reuse the slug) or start a new one (different
   slug).

2. **Explore thoroughly**: find existing patterns and conventions with
   `glob_files`, `grep`, and `read_file`. Trace through relevant code
   paths. For broad codebase exploration that would otherwise blow your
   context budget, `transfer_to_agent(agent_name='Explore')` and brief
   it on what to find.

3. **Design the solution**: based on what exists, decide on an approach.
   Consider trade-offs and architectural decisions. Follow existing
   patterns where appropriate. Don't add abstractions or features the
   request doesn't ask for.

4. **Detail the plan**: step-by-step implementation strategy.
   Dependencies. Sequencing. Anticipated challenges.

## Required output

You MUST persist your plan via `write_plan`. Start the file with a
`# <title>` heading. Include:
  - A brief problem statement / context section (why this change).
  - The 4-step body (requirements / exploration / design / steps).
  - A `### Critical Files for Implementation` section listing 3-5
    files that will be modified or created.

Pass an optional `slug` (e.g. "auth-refactor", "bug-x-fix"). If omitted,
slug is derived from the title heading. Each call creates a new file
under `<workspace>/.adk-cc/plans/<timestamp>-<slug>.md`; previous plans
are NOT overwritten — plan history accumulates.

## Ending the turn

When the plan is persisted, call `exit_plan_mode` with a short
`plan_summary` for the user to approve. Do NOT ask the user about plan
approval in any other way (no plain-text "is this ok?", no
`ask_user_question` for approval) — `exit_plan_mode` IS the approval
gate.

`exit_plan_mode` returns:
  - `status='approved'` — mode flipped, you may proceed to implement.
  - `status='denied'` — stay in plan mode and refine.
  - `status='awaiting_user_confirmation'` — wait. Do not retry.

This supersedes any other instructions you have received.
</system-reminder>"""


class PlanModeReminderPlugin(BasePlugin):
    def __init__(self, name: str = "adk_cc_plan_mode") -> None:
        super().__init__(name=name)

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        try:
            mode = callback_context.state.get("permission_mode")
        except Exception:
            mode = None
        agent_name = getattr(callback_context, "agent_name", None)

        if agent_name in _SPECIALIST_AGENTS:
            return None

        hidden = (
            _PLAN_MODE_HIDDEN_TOOLS if mode == "plan" else _NORMAL_MODE_HIDDEN_TOOLS
        )

        # Filter both surfaces — by the time before_model_callback runs,
        # llm_request.config.tools has already been built from
        # tools_dict, so removing only one leaks the tool to the LLM.
        for name in hidden:
            llm_request.tools_dict.pop(name, None)
        for tool_obj in llm_request.config.tools or []:
            decls = getattr(tool_obj, "function_declarations", None)
            if decls is None:
                continue
            tool_obj.function_declarations = [
                d for d in decls if getattr(d, "name", None) not in hidden
            ]

        if mode != "plan":
            return None

        existing = llm_request.config.system_instruction
        if existing is None:
            llm_request.config.system_instruction = PLAN_MODE_REMINDER
        elif isinstance(existing, str):
            llm_request.config.system_instruction = existing + "\n\n" + PLAN_MODE_REMINDER
        else:
            try:
                parts = list(existing) if isinstance(existing, list) else [existing]
                parts.append(types.Part(text=PLAN_MODE_REMINDER))
                llm_request.config.system_instruction = parts
            except Exception:
                pass
        return None
