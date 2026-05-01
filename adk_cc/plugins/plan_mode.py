"""Plan-mode behavioral helper plugin.

Two responsibilities, kept in one plugin since both react to the same
state flag (`permission_mode == "plan"`):

  1. **Reminder injection** — when plan mode is active, append a
     <system-reminder> to the coordinator's `system_instruction` so the
     model knows it's in planning posture. The engine's Step 2a still
     enforces; the reminder gives the rationale.
  2. **Tool visibility** — `exit_plan_mode` is only meaningful while in
     plan mode; the plugin hides it from the LLM's tool list otherwise.
     `enter_plan_mode` is hidden in the reverse direction.

Both behaviors are skipped for the read-only specialists (Plan, Explore,
verification): they're already constrained, the reminder adds tokens
without changing behavior, and the enter/exit tools are coordinator-only.
"""

from __future__ import annotations

from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

# Read-only specialists don't need the reminder or the enter/exit tools;
# they're already shaped for planning by their tool surface.
_SPECIALIST_AGENTS = frozenset({"Plan", "Explore", "verification"})


PLAN_MODE_REMINDER = (
    "<system-reminder>\n"
    "YOU ARE CURRENTLY IN PLAN MODE. Do not call `enter_plan_mode` — you are\n"
    "already in it. The user has asked you to plan rather than execute.\n"
    "\n"
    "You MUST NOT make any edits, run any non-readonly tools (no write_file,\n"
    "edit_file, run_bash, task_create, etc.), or otherwise mutate the system.\n"
    "\n"
    "Available actions in plan mode:\n"
    "  - Use read_file, glob_files, grep, web_fetch to gather context.\n"
    "  - Transfer to the `Plan` sub-agent for design work — it will call\n"
    "    `write_plan` to persist a Markdown plan under the workspace.\n"
    "  - Or call `write_plan` yourself if the plan is short and you've\n"
    "    already gathered the context.\n"
    "  - Call `read_current_plan` to see existing plans this session.\n"
    "\n"
    "When your plan is ready, call `exit_plan_mode` with a clear plan_summary.\n"
    "The user is shown your summary and must explicitly approve. The tool\n"
    "returns status='approved' (mode flipped — proceed), status='denied'\n"
    "(stay and refine), or status='awaiting_user_confirmation' (wait — do\n"
    "not retry or assume approval).\n"
    "This supersedes any other instructions you have received.\n"
    "</system-reminder>"
)


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

        # Tweak: read-only specialists are skipped entirely. They have no
        # write tools to gate, no enter/exit tools to hide, and the
        # reminder would just burn tokens.
        if agent_name in _SPECIALIST_AGENTS:
            return None

        # Tool visibility: hide enter/exit_plan_mode unless the matching
        # mode is active. By the time before_model_callback runs,
        # llm_request.config.tools has already been built from
        # tools_dict — so we have to filter BOTH places to actually
        # remove the tool from the LLM's view.
        hide_name = "enter_plan_mode" if mode == "plan" else "exit_plan_mode"
        llm_request.tools_dict.pop(hide_name, None)
        for tool_obj in llm_request.config.tools or []:
            decls = getattr(tool_obj, "function_declarations", None)
            if decls is None:
                continue
            tool_obj.function_declarations = [
                d for d in decls if getattr(d, "name", None) != hide_name
            ]

        if mode != "plan":
            return None

        # Append the reminder — preserves the agent's existing instruction.
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
