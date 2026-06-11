"""Session titles via an out-of-band LLM call (no dedicated tool).

Gives each session a human label in the UI rail ("Fizzbuzz script demo")
the way chat products do it: after the first turn completes, fire ONE tiny
separate model call ("title this conversation") and write the result to
`session.state["session_title"]` — which the sessions LIST response returns,
and the rail renders.

Why a callback and not a tool: a dedicated `set_session_title` tool pollutes
the tool surface, costs an in-band tool call, and depends on the model
remembering to call it (compliance lottery). Out-of-band titling is
guaranteed-by-construction, invisible to the agent loop, and uses a prompt we
fully control.

Mechanics:
  - `after_run_callback` fires once per invocation, AFTER all events have
    streamed but BEFORE the run completes (runners.py) — so the title is
    persisted by the time the frontend's post-turn rail refresh fetches the
    session list. Cost: one small LLM call, on the session's FIRST turn only.
  - The title is persisted the same way the frontend's PATCH route mutates
    state: append a content-less Event carrying a state_delta. Content-less
    events render nothing in the thread.
  - Self-healing: if the titling call fails (rate limit, transport), the
    session just stays untitled and the next turn retries — but only while
    the session is young (≤ _MAX_USER_TURNS user messages), so long/imported
    sessions are never retro-titled and a persistently failing model doesn't
    burn a call every turn forever.
  - Failures NEVER break the run: everything is wrapped and logged.

Registered together with ToolTitlePlugin under ADK_CC_TOOL_TITLES=1 (one
cohesive titling feature).
"""

from __future__ import annotations

import logging
from typing import Optional

from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.models.llm_request import LlmRequest
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.utils.context_utils import Aclosing
from google.genai import types

_log = logging.getLogger(__name__)

_STATE_KEY = "session_title"
_MAX_CHARS = 80
# Stop attempting once a session has more than this many user messages —
# avoids retro-titling old/imported sessions and caps retry cost when the
# titling call keeps failing.
_MAX_USER_TURNS = 4

_PROMPT_TEMPLATE = (
    "Write a short title (2-6 words) for a coding-assistant session, based "
    "on the exchange below. Reply with ONLY the title — no quotes, no "
    "trailing punctuation, no explanations.\n\n"
    "User request:\n{user}\n\n"
    "Assistant reply (excerpt):\n{agent}"
)


def _clean_title(raw: str) -> str:
    """First line, stripped of quotes/backticks/trailing punctuation,
    whitespace-collapsed, length-capped. Empty string if unusable."""
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    line = line.strip().strip("\"'`").rstrip(".!?:;,").strip()
    line = " ".join(line.split())
    if len(line) > _MAX_CHARS:
        line = line[: _MAX_CHARS - 1].rstrip() + "…"
    return line


class SessionTitlePlugin(BasePlugin):
    """Titles young, untitled sessions with one out-of-band model call."""

    def __init__(self, *, name: str = "adk_cc_session_title") -> None:
        super().__init__(name=name)

    async def after_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        try:
            await self._maybe_title(invocation_context)
        except Exception as e:  # noqa: BLE001 — titling must never break a run
            _log.warning("session_title: skipped (%s: %s)", type(e).__name__, e)

    # ---- internals -------------------------------------------------------

    @staticmethod
    def _texts(session, author_is_user: bool) -> list[str]:
        out = []
        for e in getattr(session, "events", None) or []:
            is_user = (getattr(e, "author", None) == "user")
            if is_user != author_is_user:
                continue
            content = getattr(e, "content", None)
            for p in (getattr(content, "parts", None) or []):
                if getattr(p, "thought", None):
                    continue
                t = getattr(p, "text", None)
                if t and t.strip():
                    out.append(t.strip())
        return out

    async def _maybe_title(self, ictx: InvocationContext) -> None:
        session = ictx.session
        state = getattr(session, "state", None) or {}
        if state.get(_STATE_KEY):
            return  # already titled
        user_texts = self._texts(session, author_is_user=True)
        if not user_texts or len(user_texts) > _MAX_USER_TURNS:
            return
        agent_texts = self._texts(session, author_is_user=False)

        model = getattr(ictx.agent, "canonical_model", None)
        if model is None:
            return

        prompt = _PROMPT_TEMPLATE.format(
            user=user_texts[0][:2000],
            agent=(agent_texts[-1][:500] if agent_texts else "(none)"),
        )
        req = LlmRequest(
            contents=[
                types.Content(role="user", parts=[types.Part(text=prompt)])
            ],
            config=types.GenerateContentConfig(),
        )
        raw = ""
        async with Aclosing(model.generate_content_async(req, stream=False)) as agen:
            async for resp in agen:
                content = getattr(resp, "content", None)
                for p in (getattr(content, "parts", None) or []):
                    if not getattr(p, "thought", None) and getattr(p, "text", None):
                        raw += p.text
        title = _clean_title(raw)
        if not title:
            return

        # Persist exactly like the PATCH state route: a content-less event
        # carrying a state_delta (renders nothing in the thread).
        await ictx.session_service.append_event(
            session,
            Event(
                invocation_id=ictx.invocation_id,
                author=self.name,
                actions=EventActions(state_delta={_STATE_KEY: title}),
            ),
        )
        _log.info("session_title: titled session %s: %r", session.id, title)
