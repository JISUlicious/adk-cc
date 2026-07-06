"""Session titles via an out-of-band LLM call (no dedicated tool).

Gives each session a human label in the UI rail ("Fizzbuzz script demo")
the way chat products do it: ONE tiny separate model call ("title this
request") whose result is written to `session.state["session_title"]` —
which the sessions LIST response returns, and the rail renders.

Why a callback and not a tool: a dedicated `set_session_title` tool pollutes
the tool surface, costs an in-band tool call, and depends on the model
remembering to call it (compliance lottery). Out-of-band titling is
guaranteed-by-construction, invisible to the agent loop, and uses a prompt we
fully control.

Latency design — spawn early, NEVER block the run:
  - `before_run_callback` (pipeline Step 1, once per invocation — unlike
    before_agent, which fires per agent in the tree) SPAWNS the titling call
    as an asyncio task, so it runs CONCURRENTLY with the agent's turn. The
    title is generated from the user's message alone — the reply doesn't
    exist yet, and chat products title from the first message for the same
    reason.
  - `after_run_callback` (Step 4, after events drain, before the run
    completes) persists the title — but ONLY inline when the task has already
    finished during the turn (the common case). If the title call outlived a
    short turn, after_run does NOT await it: it detaches the persist so the run
    (and the SSE stream, and the UI's "agent is working…" indicator) completes
    immediately. Awaiting here would hold the stream open for the title call's
    full latency, which on a slow model shows as a multi-second phantom
    "working" tail after the reply is already done — visible only on a session's
    first, untitled turns. Detached, the title lands a moment later and the rail
    picks it up on its next refresh.
  - Persisting only after the turn's events have drained avoids concurrent
    append_event races with the running turn.

The title is persisted the same way the frontend's PATCH route mutates state:
a content-less Event carrying a state_delta (renders nothing in the thread).

Self-healing & bounded: only untitled sessions are attempted, and only while
young (≤ _MAX_USER_TURNS user messages) — no retro-titling of old sessions,
and a persistently failing model doesn't burn a call every turn forever.
Failures NEVER break the run: everything is wrapped and logged.

Registered together with ToolTitlePlugin under ADK_CC_TOOL_TITLES=1 (one
cohesive titling feature).
"""

from __future__ import annotations

import asyncio
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
# Leak guard for the pending-task map (a crashed run can strand an entry;
# entries are tiny, but don't let them accumulate forever).
_MAX_PENDING = 32

_PROMPT_TEMPLATE = (
    "Write a short title (2-6 words) for a coding-assistant session that "
    "starts with the user request below. Reply with ONLY the title — no "
    "quotes, no trailing punctuation, no explanations.\n\n"
    "User request:\n{user}"
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
    """Titles young, untitled sessions with one out-of-band model call that
    overlaps the agent's turn (spawned at before_run, persisted at after_run).
    """

    def __init__(self, *, name: str = "adk_cc_session_title") -> None:
        super().__init__(name=name)
        # invocation_id -> in-flight title generation, spawned at before_run,
        # consumed at after_run.
        self._pending: dict[str, asyncio.Task] = {}
        # Detached persist tasks (title call outlived the turn) — held so the
        # event loop doesn't GC them mid-flight; self-discard on completion.
        self._detached: set[asyncio.Task] = set()

    # ---- spawn (Step 1: runs once, before the agent starts) --------------

    async def before_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> Optional[types.Content]:
        try:
            ictx = invocation_context
            session = ictx.session
            state = getattr(session, "state", None) or {}
            if state.get(_STATE_KEY):
                return None  # already titled
            if self._user_turn_count(session) > _MAX_USER_TURNS:
                return None  # old session — leave it
            user_text = self._content_text(ictx.user_content)
            if not user_text:
                return None
            model = getattr(ictx.agent, "canonical_model", None)
            if model is None:
                return None
            if len(self._pending) >= _MAX_PENDING:
                self._pending.clear()  # stranded entries from crashed runs
            prompt = _PROMPT_TEMPLATE.format(user=user_text[:2000])
            # Concurrent with the agent's turn — NOT awaited here.
            self._pending[ictx.invocation_id] = asyncio.create_task(
                self._generate(model, prompt)
            )
        except Exception as e:  # noqa: BLE001 — titling must never break a run
            _log.warning("session_title: spawn skipped (%s: %s)", type(e).__name__, e)
        return None

    # ---- persist (Step 4: after events drain, before the run completes) --

    async def after_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        ictx = invocation_context
        task = self._pending.pop(ictx.invocation_id, None)
        if task is None:
            return
        if task.done():
            # Finished during the turn → persist inline (no added latency).
            await self._persist(ictx.session, ictx.session_service, ictx.invocation_id, task)
        else:
            # Still running: do NOT await it here — that would hold the SSE stream
            # (and the "agent is working…" indicator) open for the rest of the
            # title call. Persist in a detached task so the run completes now.
            t = asyncio.create_task(
                self._persist(ictx.session, ictx.session_service, ictx.invocation_id, task)
            )
            self._detached.add(t)
            t.add_done_callback(self._detached.discard)

    async def _persist(self, session, session_service, invocation_id: str, task: asyncio.Task) -> None:
        """Await the title task and write it to session state (once)."""
        try:
            title = await task
            if not title:
                return
            if (getattr(session, "state", None) or {}).get(_STATE_KEY):
                return  # raced by a concurrent invocation — keep the first
            await session_service.append_event(
                session,
                Event(
                    invocation_id=invocation_id,
                    author=self.name,
                    actions=EventActions(state_delta={_STATE_KEY: title}),
                ),
            )
            _log.info("session_title: titled session %s: %r", session.id, title)
        except Exception as e:  # noqa: BLE001
            _log.warning("session_title: persist skipped (%s: %s)", type(e).__name__, e)

    # ---- internals -------------------------------------------------------

    @staticmethod
    def _content_text(content: Optional[types.Content]) -> str:
        parts = getattr(content, "parts", None) or []
        return "\n".join(
            p.text.strip()
            for p in parts
            if not getattr(p, "thought", None)
            and getattr(p, "text", None)
            and p.text.strip()
        )

    @staticmethod
    def _user_turn_count(session) -> int:
        n = 0
        for e in getattr(session, "events", None) or []:
            if getattr(e, "author", None) != "user":
                continue
            content = getattr(e, "content", None)
            if any(
                getattr(p, "text", None) and p.text.strip()
                for p in (getattr(content, "parts", None) or [])
            ):
                n += 1
        return n

    async def _generate(self, model, prompt: str) -> str:
        """The out-of-band model call. Returns '' on any failure."""
        try:
            req = LlmRequest(
                contents=[
                    types.Content(role="user", parts=[types.Part(text=prompt)])
                ],
                config=types.GenerateContentConfig(),
            )
            raw = ""
            async with Aclosing(
                model.generate_content_async(req, stream=False)
            ) as agen:
                async for resp in agen:
                    content = getattr(resp, "content", None)
                    for p in (getattr(content, "parts", None) or []):
                        if not getattr(p, "thought", None) and getattr(p, "text", None):
                            raw += p.text
            return _clean_title(raw)
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "session_title: generation failed (%s: %s)", type(e).__name__, e
            )
            return ""
