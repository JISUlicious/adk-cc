"""Ask the user a multi-choice question mid-session.

Marked `long_running=True`. The pause is delivered by two things working
together:
  1. ADK sets `long_running_tool_ids` on the function-CALL event from
     the tool's `is_long_running` flag (base_llm_flow.py:106).
  2. `AdkCcTool.run_async` sets `tool_context.actions.skip_summarization
     = True` after `_execute` returns. Without (2), the function-RESPONSE
     event isn't marked final and the loop cascades — see the comment in
     `adk_cc/tools/base.py`.

Output shape (the LLM consumes this on resume):
    {
      "status": "answered" | "cancelled",
      "answers": { "<question text>": "<chosen label>" | ["<l1>", "<l2>"] },
    }

For the initial pause, the tool returns:
    {
      "status": "awaiting_user_input",
      "questions": [...],
    }

The frontend that wires up `adk web` is expected to render the questions
and POST the answer back as a function_response with the same call_id.
Without a frontend that knows this protocol, the call will time out —
the contract is documented; operators wire it.
"""

from __future__ import annotations

from typing import Any

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
    ) -> dict[str, Any]:
        # The actual answer arrives via ADK's long-running tool resumption
        # path, which replaces this initial payload with the user's response.
        # Until that arrives, the LLM sees a deterministic awaiting state.
        return {
            "status": "awaiting_user_input",
            "questions": [q.model_dump() for q in args.questions],
        }
