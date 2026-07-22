"""Per-session model selection — session state → SelectableLlm override.

`/model` in chat pins a model FOR THAT SESSION by writing two session-state
keys (via the standard `PATCH /apps/…/sessions/{id}` state_delta route, the
same no-turn write path the plan-mode toggle uses):

    state["model_endpoint"] = "<registry endpoint name>"
    state["model_id"]       = "<full model id offered by that endpoint>"

This plugin bridges that state to the model layer: on EVERY before_model
callback it copies the pair into `selectable.set_session_model_override`
(a contextvar) — or clears it when the session has no pin — so
`SelectableLlm._resolve_delegate` picks the session's endpoint for exactly
the model calls of this task. ADK runs the callback and the model call in
the same async task (base_llm_flow), and the always-set discipline means a
reused task can never leak a previous session's choice.

Scope notes:
  - Sub-agents (Explore, verification) share the session's state → the whole
    turn follows the session's model. Intended.
  - Out-of-band model users (memory scheduler, session titles) call the
    global MODEL outside any before_model callback → global default; tasks
    spawned INSIDE a turn inherit the contextvar (asyncio copies context) and
    follow the session's model. Both acceptable.
  - Settings → Models keeps managing the GLOBAL default (registry active
    pointer); unset state keys mean "follow the default" — behavior is
    byte-identical to before this plugin existed.
"""

from __future__ import annotations

from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin

from ..models.selectable import set_session_model_override

STATE_ENDPOINT_KEY = "model_endpoint"
STATE_MODEL_KEY = "model_id"


class ModelSessionPlugin(BasePlugin):
    """Copies the session's pinned model (if any) into the model-layer
    contextvar before every model call."""

    def __init__(self, name: str = "adk_cc_model_session") -> None:
        super().__init__(name=name)

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        endpoint = model_id = None
        try:
            state = callback_context.state
            endpoint = state.get(STATE_ENDPOINT_KEY)
            model_id = state.get(STATE_MODEL_KEY)
        except Exception:  # noqa: BLE001 — a state hiccup must not block the turn
            pass
        if endpoint:
            set_session_model_override((str(endpoint), str(model_id or "")))
        else:
            # ALWAYS clear when unpinned — the contextvar must reflect THIS
            # call's session, never a previous one on a reused task.
            set_session_model_override(None)
        return None
