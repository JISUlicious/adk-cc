"""Tests for SessionTitlePlugin (plugins/session_title.py).

Out-of-band session titling, spawn-early/persist-late: before_run spawns the
titling model call CONCURRENTLY with the agent turn; after_run awaits it
(already done) and persists state["session_title"] via a content-less
state-delta event. Uses a fake BaseLlm — no live model. Hand-rolled.
"""

from __future__ import annotations

import asyncio
import os
import time
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
    """Returns a canned title after `delay`s; counts calls; can explode."""

    reply: str = '  "Fizzbuzz Script Demo."  \nignored second line'
    delay: float = 0.0
    explode: bool = False
    calls: int = 0

    async def generate_content_async(
        self, llm_request, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        type(self).calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
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


async def _make_ictx(*events: Event, state=None, llm: _FakeLlm = None,
                     user_text="make a fizzbuzz script"):
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="t", user_id="u", state=state or {})
    for e in events:
        await svc.append_event(session, e)
    agent = LlmAgent(name="t", model=llm or _FakeLlm(model="fake/model"))
    user_content = (
        types.Content(role="user", parts=[types.Part(text=user_text)])
        if user_text is not None else None
    )
    return InvocationContext(
        session_service=svc, invocation_id="inv-run",
        agent=agent, session=session, user_content=user_content,
    ), svc, session


async def _full_run(plugin: SessionTitlePlugin, ictx):
    await plugin.before_run_callback(invocation_context=ictx)
    await plugin.after_run_callback(invocation_context=ictx)


def test_titles_after_first_turn():
    async def run():
        _FakeLlm.calls = 0
        ictx, svc, session = await _make_ictx(_user_event("make a fizzbuzz script"))
        await _full_run(SessionTitlePlugin(), ictx)
        fresh = await svc.get_session(app_name="t", user_id="u", session_id=session.id)
        # quotes/period/second-line stripped by _clean_title
        assert fresh.state.get("session_title") == "Fizzbuzz Script Demo", fresh.state
        assert _FakeLlm.calls == 1
        # persisted via a content-less event (renders nothing in the thread)
        last = fresh.events[-1]
        assert last.author == "adk_cc_session_title" and last.content is None
    asyncio.run(run())
    print("OK titles_after_first_turn")


def test_generation_overlaps_the_turn():
    """The titling call runs CONCURRENTLY with the (simulated) agent turn:
    a 0.3s title call + a 0.3s turn complete in ~0.3s, not ~0.6s."""
    async def run():
        _FakeLlm.calls = 0
        ictx, svc, session = await _make_ictx(
            _user_event("hi"), llm=_FakeLlm(model="fake/model", delay=0.3))
        plugin = SessionTitlePlugin()
        t0 = time.perf_counter()
        await plugin.before_run_callback(invocation_context=ictx)  # spawn
        await asyncio.sleep(0.3)                                   # the "turn"
        await plugin.after_run_callback(invocation_context=ictx)   # persist
        elapsed = time.perf_counter() - t0
        fresh = await svc.get_session(app_name="t", user_id="u", session_id=session.id)
        assert fresh.state.get("session_title"), fresh.state
        assert elapsed < 0.5, f"not overlapped: {elapsed:.3f}s (~0.6s = serial)"
        print(f"  (0.3s call + 0.3s turn = {elapsed:.3f}s total — overlapped)")
    asyncio.run(run())
    print("OK generation_overlaps_the_turn")


def test_skips_when_already_titled():
    async def run():
        _FakeLlm.calls = 0
        ictx, svc, session = await _make_ictx(
            _user_event("hello"), state={"session_title": "Existing"})
        await _full_run(SessionTitlePlugin(), ictx)
        fresh = await svc.get_session(app_name="t", user_id="u", session_id=session.id)
        assert fresh.state["session_title"] == "Existing"
        assert _FakeLlm.calls == 0, "must not even spawn the model call"
    asyncio.run(run())
    print("OK skips_when_already_titled")


def test_skips_without_user_content():
    async def run():
        _FakeLlm.calls = 0
        ictx, svc, session = await _make_ictx(user_text=None)
        await _full_run(SessionTitlePlugin(), ictx)
        fresh = await svc.get_session(app_name="t", user_id="u", session_id=session.id)
        assert "session_title" not in fresh.state
        assert _FakeLlm.calls == 0
    asyncio.run(run())
    print("OK skips_without_user_content")


def test_skips_old_sessions():
    async def run():
        _FakeLlm.calls = 0
        events = [_user_event(f"msg {i}", i) for i in range(6)]  # > _MAX_USER_TURNS
        ictx, svc, session = await _make_ictx(*events)
        await _full_run(SessionTitlePlugin(), ictx)
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
        await _full_run(SessionTitlePlugin(), ictx)  # must not raise
        fresh = await svc.get_session(app_name="t", user_id="u", session_id=session.id)
        assert "session_title" not in fresh.state
    asyncio.run(run())
    print("OK model_failure_never_breaks_run")


def test_after_run_without_spawn_is_noop():
    async def run():
        ictx, svc, session = await _make_ictx(_user_event("hi"))
        # after_run alone (e.g. plugin hot-swapped mid-run) — no pending task
        await SessionTitlePlugin().after_run_callback(invocation_context=ictx)
        fresh = await svc.get_session(app_name="t", user_id="u", session_id=session.id)
        assert "session_title" not in fresh.state
    asyncio.run(run())
    print("OK after_run_without_spawn_is_noop")


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
    test_generation_overlaps_the_turn()
    test_skips_when_already_titled()
    test_skips_without_user_content()
    test_skips_old_sessions()
    test_model_failure_never_breaks_run()
    test_after_run_without_spawn_is_noop()
    test_clean_title()
    print("\nall session-title-plugin tests passed")


if __name__ == "__main__":
    main()
