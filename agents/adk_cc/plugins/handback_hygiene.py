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
"""

from __future__ import annotations

from typing import Optional

from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
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
