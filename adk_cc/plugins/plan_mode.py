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
    "Plan mode is active. The user has asked you to plan rather than execute.\n"
    "You MUST NOT make any edits, run any non-readonly tools (including changing\n"
    "configs, running commits, or starting background tasks), or otherwise mutate\n"
    "the system. Read tools (read_file, glob_files, grep, web_fetch) and the\n"
    "Explore/Plan sub-agents are still available.\n"
    "\n"
    "When your plan is ready, call `exit_plan_mode` with a clear plan_summary.\n"
    "The user is shown your summary and must explicitly approve. The tool returns\n"
    "with status='approved' (mode flipped — proceed) or status='denied' (stay in\n"
    "plan mode — refine and re-request) or status='awaiting_user_confirmation'\n"
    "(the runtime is waiting on the user — do not retry or assume approval).\n"
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
        # mode is active. Mutating tools_dict before the request leaves
        # ADK's downstream config-building to drop the tool from the
        # function-declaration list automatically.
        if mode == "plan":
            llm_request.tools_dict.pop("enter_plan_mode", None)
        else:
            llm_request.tools_dict.pop("exit_plan_mode", None)

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
