"""Ask the user a multi-choice question mid-session.

Marked `long_running=True` so ADK pauses the tool result and surfaces
the question payload to the frontend. The frontend collects the answer
and submits it back as the function-call response.

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
and POST the answer back. Without a frontend that knows this protocol,
the call will time out — the contract is documented; operators wire it.
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
