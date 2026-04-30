"""Plan-mode reminder injector.

adk-cc enforces plan mode at the engine level (`permissions/engine.py`
Step 2a), but upstream Claude Code does it via prompt reminders. This
plugin mirrors the upstream pattern *on top of* the engine — the
reminder gives the model the "why" so it understands the gate it's
hitting; the engine remains the structural floor.

Activation: when `tool_context.state["permission_mode"] == "plan"`, the
plugin appends a reminder to `llm_request.config.system_instruction`
on every model call. The text mirrors upstream's `getPlanModeV2Instructions`
language (`utils/messages.ts:3227` in the upstream source).

The user exits plan mode by calling `exit_plan_mode` (the tool added in
this stage), which flips the state key back to "default".
"""

from __future__ import annotations

from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types


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
        if mode != "plan":
            return None

        # Append rather than replace — preserves the agent's instruction.
        existing = llm_request.config.system_instruction
        if existing is None:
            llm_request.config.system_instruction = PLAN_MODE_REMINDER
        elif isinstance(existing, str):
            llm_request.config.system_instruction = existing + "\n\n" + PLAN_MODE_REMINDER
        else:
            # Content / Part / list-of-Parts — append as a new Part.
            try:
                parts = list(existing) if isinstance(existing, list) else [existing]
                parts.append(types.Part(text=PLAN_MODE_REMINDER))
                llm_request.config.system_instruction = parts
            except Exception:
                # Last-resort fallback: leave the existing instruction alone.
                pass
        return None
