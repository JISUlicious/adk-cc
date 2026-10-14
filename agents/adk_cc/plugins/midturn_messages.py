"""Deliver messages the user sends while a turn is running (mid-turn queue).

The Turn Broker queues them on the running turn (`Turn.queue`). This plugin
drains that queue at the next MODEL STEP: `before_model_callback` runs after
the step's request processors — i.e. after the previous tool results are
already in the session — which is the only safe boundary. Injecting between a
function call and its response is an API error upstream warns about.

Each drained message is (1) persisted as an ordinary user event under the
running invocation, so history, memory capture and the UI all see it, and
(2) appended to THIS request, whose contents ADK already built before the
callback ran. Later steps pick it up from the session naturally.

Scope: the coordinator of the turn's own session only. Specialists that hold
control (transfer_to_agent) and spawned sub-agents never receive user
messages; the queue waits until the coordinator's next step, or becomes a
follow-up turn if the coordinator already gave its last reply.
"""
from __future__ import annotations

import logging

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

_log = logging.getLogger(__name__)

_COORDINATOR = "coordinator"
MIDTURN_METADATA_KEY = "adk_cc_midturn"

MIDTURN_RAW_KEY = "adk_cc_midturn_raw"

# The framing is PERSISTED in the event text, not added to one request only:
# ADK rebuilds the model's input from session events at every step, so a
# request-only frame vanished one call later and the message then read as a
# bare user turn wedged after a tool result. The UI shows the raw text (kept
# in metadata). Policy merges upstream's guard ("you MUST address it, do not
# ignore it") with redirect-now when the message changes the task.
def frame(text: str) -> str:
    return (
        "<system-reminder>The user sent a new message while you were working:"
        "</system-reminder>\n"
        f"{text}\n"
        "<system-reminder>If it changes, narrows or cancels what you are doing, "
        "adjust now — stop work it makes obsolete. Otherwise finish the step you "
        "are on, then address it before you end the turn. Never silently ignore "
        "it: say in your reply how you handled it.</system-reminder>"
    )


class MidTurnMessagesPlugin(BasePlugin):
    def __init__(self, *, name: str = "adk_cc_midturn_messages") -> None:
        super().__init__(name=name)

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        try:
            from ..service.turns import CURRENT_TURN

            turn = CURRENT_TURN.get()
            if turn is None or not turn.queue:
                return None
            if getattr(callback_context, "agent_name", None) != _COORDINATOR:
                return None
            ictx = callback_context._invocation_context
            if getattr(ictx.session, "id", None) != turn.session_id:
                return None          # a spawned sub-agent's own session
            items, turn.queue = list(turn.queue), []   # take synchronously
            for item in items:
                await self._deliver(ictx, turn, llm_request, item)
            _log.info("mid-turn: delivered %d message(s) to turn %s",
                      len(items), turn.id)
        except Exception as e:  # noqa: BLE001 — never break a model call
            _log.warning("mid-turn delivery skipped (%s: %s)",
                         type(e).__name__, e)
        return None

    async def _deliver(self, ictx, turn, llm_request: LlmRequest, item: dict) -> None:  # noqa: ANN001
        from google.adk.events.event import Event

        text = item["text"]
        framed = frame(text)
        event = Event(
            invocation_id=ictx.invocation_id,
            author="user",
            content=types.Content(role="user", parts=[types.Part(text=framed)]),
            custom_metadata={MIDTURN_METADATA_KEY: item["id"],
                             MIDTURN_RAW_KEY: text},
        )
        await ictx.session_service.append_event(ictx.session, event)
        await turn.push(event.model_dump_json(exclude_none=True, by_alias=True),
                        model_authored=False)
        llm_request.contents.append(
            types.Content(role="user", parts=[types.Part(text=framed)]))
