"""Make `ask_user_question` reliably pause the turn for the user's answer.

ADK pauses the agent loop for a long-running tool ONLY when its function-CALL
event is the turn's final event — i.e. when the long-running call is the sole/last
call. But a model sometimes emits `ask_user_question` ALONGSIDE other tool calls
in one turn (observed: `run_bash` + `ask_user_question`). The siblings execute and
their `functionResponse` events re-invoke the model, so the pending ask is never
awaited — the agent barrels ahead on assumptions instead of waiting for the answer.

Fix at the model-response layer (same hook + in-place-mutation style as
`AskUserQuestionUiHintPlugin`): when a model turn contains an `ask_user_question`
call, DROP the other function-call parts from the response so the ask is the SOLE
call. ADK then sets `long_running_tool_ids` on just that call, its CALL event is
`is_final_response()`, and the loop pauses until the user answers. The siblings are
never dispatched; the model re-decides them (if still needed) on resume, now with
the answer in hand. Text/thought parts are kept — only sibling function calls go.

This is the hard guarantee; the plan-mode reminder + the tool description also ask
the model to call `ask_user_question` alone (defense in depth), but prompts are a
compliance lottery, so this plugin is what actually enforces the pause.
"""

from __future__ import annotations

import logging
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin

_log = logging.getLogger(__name__)

ASK_USER_QUESTION_TOOL_NAME = "ask_user_question"


class AskPausePlugin(BasePlugin):
    """When a turn contains `ask_user_question`, strip sibling tool calls so the
    ask is the only call and the loop pauses for the user's answer."""

    def __init__(self, name: str = "adk_cc_ask_pause") -> None:
        super().__init__(name=name)

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> Optional[LlmResponse]:
        content = getattr(llm_response, "content", None)
        parts = getattr(content, "parts", None) if content is not None else None
        if not parts:
            return None

        # Is ask_user_question one of this turn's function calls?
        def _is_ask(part) -> bool:
            fc = getattr(part, "function_call", None)
            return fc is not None and fc.name == ASK_USER_QUESTION_TOOL_NAME

        if not any(_is_ask(p) for p in parts):
            return None

        # Keep text/thought parts and the ask call; drop every OTHER function call.
        kept = []
        dropped: list[str] = []
        for p in parts:
            fc = getattr(p, "function_call", None)
            if fc is not None and fc.name != ASK_USER_QUESTION_TOOL_NAME:
                dropped.append(fc.name)
                continue
            kept.append(p)

        if not dropped:
            return None  # the ask was already alone — nothing to do

        content.parts = kept
        _log.info(
            "ask_pause: deferred %d sibling call(s) %s so ask_user_question pauses "
            "the turn (the model re-decides them after the user answers).",
            len(dropped), dropped,
        )
        # In-place mutation; returning None tells ADK to use the (modified) response.
        return None
