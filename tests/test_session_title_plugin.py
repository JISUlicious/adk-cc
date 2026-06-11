"""Tests for SessionTitlePlugin (plugins/session_title.py).

Out-of-band session titling: after_run_callback fires one small model call
and persists state["session_title"] via a content-less state-delta event.
Uses a fake BaseLlm — no live model. Hand-rolled (no pytest).
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncGenerator

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from adk_cc.plugins.session_title import SessionTitlePlugin, _clean_title


class _FakeLlm(BaseLlm):
    """Returns a canned title; counts calls; can be told to explode."""

    reply: str = '  "Fizzbuzz Script Demo."  \nignored second line'
    explode: bool = False
    calls: int = 0

    async def generate_content_async(
        self, llm_request, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        type(self).calls += 1
        self.calls = type(self).calls
        if self.explode:
            raise RuntimeError("model down")
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text=self.reply)]
            )
        )


def _user_event(text: str, n: int = 0) -> Event:
    return Event(
        invocation_id=f"inv-u{n}", author="user",
        content=types.Content(role="user", parts=[types.Part(text=text)]),
    )


def _agent_event(text: str) -> Event:
    return Event(
        invocation_id="inv-a", author="coordinator",
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


async def _make_ictx(*events: Event, state=None, llm: _FakeLlm = None):
    svc = InMemorySessionService()
    session = await svc.create_session(
        app_name="t", user_id="u", state=state or {}
    )
    for e in events:
        await svc.append_event(session, e)
    agent = LlmAgent(name="t", model=llm or _FakeLlm(model="fake/model"))
    return InvocationContext(
        session_service=svc, invocation_id="inv-run",
        agent=agent, session=session,
    ), svc, session


def test_titles_after_first_turn():
    async def run():
        _FakeLlm.calls = 0
        ictx, svc, session = await _make_ictx(
            _user_event("make a fizzbuzz script"),
            _agent_event("Done — fizzbuzz.py created."),
        )
        await SessionTitlePlugin().after_run_callback(invocation_context=ictx)
        fresh = await svc.get_session(
            app_name="t", user_id="u", session_id=session.id)
        # quotes/period/second-line stripped by _clean_title
        assert fresh.state.get("session_title") == "Fizzbuzz Script Demo", fresh.state
        assert _FakeLlm.calls == 1
        # persisted via a content-less event (renders nothing in the thread)
        last = fresh.events[-1]
        assert last.author == "adk_cc_session_title" and last.content is None
    asyncio.run(run())
    print("OK titles_after_first_turn")


def test_skips_when_already_titled():
    async def run():
        _FakeLlm.calls = 0
        ictx, svc, session = await _make_ictx(
            _user_event("hello"), state={"session_title": "Existing"})
        await SessionTitlePlugin().after_run_callback(invocation_context=ictx)
        fresh = await svc.get_session(app_name="t", user_id="u", session_id=session.id)
        assert fresh.state["session_title"] == "Existing"
        assert _FakeLlm.calls == 0, "must not burn a model call"
    asyncio.run(run())
    print("OK skips_when_already_titled")


def test_skips_without_user_message():
    async def run():
        _FakeLlm.calls = 0
        ictx, svc, session = await _make_ictx(_agent_event("system warmup"))
        await SessionTitlePlugin().after_run_callback(invocation_context=ictx)
        fresh = await svc.get_session(app_name="t", user_id="u", session_id=session.id)
        assert "session_title" not in fresh.state
        assert _FakeLlm.calls == 0
    asyncio.run(run())
    print("OK skips_without_user_message")


def test_skips_old_sessions():
    async def run():
        _FakeLlm.calls = 0
        events = [_user_event(f"msg {i}", i) for i in range(6)]  # > _MAX_USER_TURNS
        ictx, svc, session = await _make_ictx(*events)
        await SessionTitlePlugin().after_run_callback(invocation_context=ictx)
        fresh = await svc.get_session(app_name="t", user_id="u", session_id=session.id)
        assert "session_title" not in fresh.state
        assert _FakeLlm.calls == 0
    asyncio.run(run())
    print("OK skips_old_sessions")


def test_model_failure_never_breaks_run():
    async def run():
        _FakeLlm.calls = 0
        ictx, svc, session = await _make_ictx(
            _user_event("hi"), llm=_FakeLlm(model="fake/model", explode=True))
        # must not raise
        await SessionTitlePlugin().after_run_callback(invocation_context=ictx)
        fresh = await svc.get_session(app_name="t", user_id="u", session_id=session.id)
        assert "session_title" not in fresh.state
    asyncio.run(run())
    print("OK model_failure_never_breaks_run")


def test_clean_title():
    assert _clean_title('"Fizzbuzz Demo."') == "Fizzbuzz Demo"
    assert _clean_title("  A   spaced   out\ttitle  ") == "A spaced out title"
    assert _clean_title("first line\nsecond") == "first line"
    assert _clean_title("   ") == ""
    long = _clean_title("x" * 200)
    assert len(long) <= 80 and long.endswith("…")
    print("OK clean_title")


def main():
    test_titles_after_first_turn()
    test_skips_when_already_titled()
    test_skips_without_user_message()
    test_skips_old_sessions()
    test_model_failure_never_breaks_run()
    test_clean_title()
    print("\nall session-title-plugin tests passed")


if __name__ == "__main__":
    main()
