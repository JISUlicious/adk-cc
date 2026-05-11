"""Ask the user a multi-choice question mid-session.

Marked `long_running=True`. The pause is delivered by ADK's long-running
machinery + the bundled `adk web` UI's response widget:

  1. ADK sets `long_running_tool_ids` on the function-CALL event from the
     tool's `is_long_running` flag (base_llm_flow.py:106). This alone
     makes the call event `is_final_response()` → the loop pauses.

  2. `_execute` returns `None`, NOT a dict. ADK's tool dispatcher
     (`functions.py:578-582`) short-circuits the response-event build
     when a long-running tool returns falsy:

        if tool.is_long_running:
            if not function_response:
                return None

     Without this, returning `{"status": "awaiting_user_input", ...}`
     produces an immediate function_response event. The bundled `adk
     web` UI's `app-long-running-response` widget renders only when
     `needsResponse && !hasFunctionResponse(callId)` — so as soon as
     the response event lands, the widget hides itself. Returning None
     keeps the call "pending" so the widget stays visible until the
     user actually submits an answer.

  3. The bundled UI surfaces a structured form for each question because
     `AskUserQuestionUiHintPlugin` injects a `response_schema` into the
     function-call args after the LLM emits the call. Without that hint,
     the UI falls back to a free-form text/JSON textarea.

When the user submits, the bundled UI POSTs a function_response with the
same call_id whose `response` is `{<question text>: <chosen label>}` (or
the array variant for multi_select). That becomes the first response for
the call and the LLM consumes it on the next turn.

Without a frontend that knows the long-running response protocol, the
call will hang forever — there's no auto-timeout. Operators wire either
the bundled `adk web` UI or a compatible custom frontend.
"""

from __future__ import annotations

from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext

from .base import AdkCcTool, ToolMeta
from .schemas import AskUserQuestionArgs


class AskUserQuestionTool(AdkCcTool):
    meta = ToolMeta(
        name="ask_user_question",
        is_read_only=True,
        is_concurrency_safe=False,
        long_running=True,
    )
    input_model = AskUserQuestionArgs
    description = (
        "Ask the user 1-4 multi-choice questions. The agent loop pauses "
        "until the user answers via the frontend. Use sparingly — only "
        "when the next step genuinely depends on user input."
    )

    async def _execute(
        self, args: AskUserQuestionArgs, ctx: ToolContext
    ) -> Optional[dict[str, Any]]:
        # Return None so ADK skips building the function_response event.
        # The function-CALL event already has `long_running_tool_ids` set
        # (ADK does that from `meta.long_running=True`), which is enough
        # to pause the loop AND keep the bundled UI's response widget
        # visible (see module docstring for the full mechanism).
        return None
