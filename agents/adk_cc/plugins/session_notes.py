"""NotesPlugin (#127): inject the session's curated notes every turn.

Same injection mechanics as memory recall — a request-time system-
instruction block, never an event — which is the entire survival story:
event compaction folds history, but this block is rebuilt from session
state on every call. Injected AFTER memory recall so the model reads
"who the user is" before "where this session stands".
"""
from __future__ import annotations

import logging
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin

from ..tools.session_notes import STATE_KEY, notes_budget_chars

_log = logging.getLogger(__name__)


class NotesPlugin(BasePlugin):
    def __init__(self, name: str = "adk_cc_session_notes") -> None:
        super().__init__(name=name)

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        try:
            notes = str(callback_context.state.get(STATE_KEY) or "").strip()
            if not notes:
                return None
            block = ("\n\n## Session notes (yours — update with "
                     "update_session_notes)\n" + notes[: notes_budget_chars()])
            from .memory import _append_to_system_instruction

            _append_to_system_instruction(llm_request, block)
        except Exception as e:  # noqa: BLE001 — never break a turn over notes
            _log.warning("session notes injection skipped (%s: %s)",
                         type(e).__name__, e)
        return None
