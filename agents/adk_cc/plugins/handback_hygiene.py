"""Strip auto-generated responses to the synthetic handback marker.

The coordinator forces a post-specialist synthesis step by having each
specialist's after_agent_callback emit a `_handback_to_coordinator` function
CALL (see agent._force_coordinator_continuation). With ADK resumability ON,
the step-runner auto-executes any trailing unanswered function call
(base_llm_flow: "is_resumable and events[-1].get_function_calls()"), so every
specialist turn now grows a coordinator-authored function RESPONSE to that
marker.

That response is poison for the next contents assembly: the marker CALL is a
FOREIGN event for the coordinator (narrated to plain text by
`_convert_foreign_event`), while the coordinator's own RESPONSE stays a real
functionResponse — ADK's `_rearrange_events_for_latest_function_response`
then aborts the whole turn with `No function call event found for function
responses ids: {'handback-…'}`.

This plugin drops handback function_response parts from events before they
persist. The flow's own loop decision already happened on the original
object, so the coordinator still takes its synthesis step; history just
never contains the unpairable response.

The CALL, however, must STAY in history: the turn broker detects a run that
ended on an unanswered marker (`service.turns._is_dangling_handback`) and
auto-continues the coordinator. Delete it and F3 resumed turns hang.

So the second half of the hygiene happens at request-assembly time instead.
Tracing a real re-entry request showed why it is needed: the surviving CALL is
harmless for the coordinator (a FOREIGN event, narrated to plain text) but when
a sub-agent is transferred to a SECOND time in the same turn, it sees its OWN
unanswered call on its branch —

    model: CALL glob_files#g1
    user:  RESP glob_files#g1
    model: text(26)
    model: CALL _handback_to_coordinator#handback-0     <- nothing answers this
    user:  text(...)

— the malformed shape that makes a real model return empty. Live, that made
verification's second run do nothing at all (no tools, no text) while the
coordinator told the user a verdict was on its way. `before_model_callback`
strips the marker from the OUTGOING contents only, so history stays intact for
the broker and every request stays well-formed.
"""

from __future__ import annotations

from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

_HANDBACK = "_handback_to_coordinator"


class HandbackHygienePlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="handback_hygiene")

    async def on_event_callback(
        self, *, invocation_context: InvocationContext, event: Event
    ) -> Optional[Event]:
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) or []
        keep = [
            p for p in parts
            if not (getattr(p, "function_response", None) is not None
                    and p.function_response.name == _HANDBACK)
        ]
        if len(keep) == len(parts):
            return None  # untouched
        # Return a COPY: the flow still holds the original object and decides
        # "loop or stop" on it AFTER this hook — in-place stripping would make
        # the event look final and cut the coordinator's synthesis step. The
        # copy is what gets persisted/streamed.
        stripped = event.model_copy(deep=True)
        stripped.content = (
            types.Content(role=content.role, parts=keep) if keep else None
        )
        return stripped

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        """Drop the marker from the outgoing contents. See module doc.

        Never returns a response — this only sanitises the request in place.
        """
        contents = getattr(llm_request, "contents", None)
        if not contents:
            return None
        cleaned = []
        for c in contents:
            parts = getattr(c, "parts", None) or []
            keep = [p for p in parts if not _is_marker(p)]
            if len(keep) == len(parts):
                cleaned.append(c)
                continue
            if keep:  # the marker rode along with real parts — keep those
                cleaned.append(types.Content(role=c.role, parts=keep))
        if len(cleaned) != len(contents):
            llm_request.contents = cleaned
        return None


def _is_marker(part) -> bool:
    fc = getattr(part, "function_call", None)
    fr = getattr(part, "function_response", None)
    return (fc is not None and fc.name == _HANDBACK) or (
        fr is not None and fr.name == _HANDBACK
    )
