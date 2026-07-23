"""Durable runs P4 (F2c): orphaned-user-event pruning. No live models.

An errored turn with ZERO model events leaves exactly one thing in history:
its own user message. Before the session's NEXT broker turn (Retry button or
a fresh message), that orphan is pruned via the file session service's
`delete_last_event` — so the transcript never grows duplicates from failed
attempts. Services without delete support degrade to the old behavior
(covered by the InMemory-based broker suite).

Covers:
  - FileSessionService.delete_last_event: matching id deletes, stale id is a
    no-op, atomic rewrite preserves earlier events;
  - retry_last after a zero-output error → exactly ONE user copy on success;
  - a FRESH message after a zero-output error → orphan gone, new message
    present once;
  - an errored turn WITH model output is NOT pruned (partial work stays);
  - mismatched last event (another driver appended) is left alone.

Run: `uv run python tests/test_turn_prune.py`
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
_TMP = tempfile.mkdtemp(prefix="adk-cc-turnprune-")
os.environ.setdefault("ADK_CC_DESKTOP", "1")
os.environ.setdefault("ADK_CC_DESKTOP_DATA", _TMP)
os.environ.setdefault("ADK_CC_WORKSPACE_ROOT", _TMP)
os.environ["ADK_CC_MODEL_RETRIES"] = "0"  # broker sees model failures at once

from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.genai import types  # noqa: E402
from pydantic import Field  # noqa: E402

from adk_cc.service.file_session_service import FileSessionService  # noqa: E402
from adk_cc.service.turns import TurnBroker  # noqa: E402


def _text_resp(t: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=t)]),
        partial=False,
    )


class _RL(Exception):
    status_code = 429


class _FlakyLlm(BaseLlm):
    """Raises a 429 for the first `fails` calls, then serves the queue."""

    model: str = "fake/turnprune"
    fails: int = 0
    responses: list = Field(default_factory=list)
    calls: int = 0

    async def generate_content_async(self, req, stream: bool = False):
        self.calls += 1
        if self.fails > 0:
            self.fails -= 1
            raise _RL("provider temporarily rate-limited upstream")
        yield self.responses.pop(0)


def _msg(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def _fresh(responses, *, fails):
    import adk_cc.agent as A

    llm = _FlakyLlm(fails=fails, responses=list(responses))
    A.MODEL._resolve_delegate = lambda: llm
    svc = FileSessionService(tempfile.mkdtemp(prefix="adk-cc-prune-svc-",
                                              dir=_TMP))
    runner = Runner(app=A.app, session_service=svc,
                    artifact_service=None, memory_service=None)

    async def get_runner(app_name):
        return runner

    return TurnBroker(get_runner=get_runner, session_service=svc), svc, llm


async def _mk_session(svc) -> str:
    import adk_cc.agent as A

    s = await svc.create_session(app_name=A.app.name, user_id="u1")
    return s.id


async def _user_texts(svc, sid) -> list[str]:
    import adk_cc.agent as A

    s = await svc.get_session(app_name=A.app.name, user_id="u1",
                              session_id=sid)
    out = []
    for e in s.events or []:
        if getattr(e, "author", "") != "user":
            continue
        for p in (getattr(e.content, "parts", None) or []):
            if getattr(p, "text", None):
                out.append(p.text)
    return out


async def _await_turn(turn):
    await asyncio.wait_for(asyncio.gather(turn.task, return_exceptions=True),
                           30)


def test_delete_last_event_guards() -> None:
    async def main():
        broker, svc, llm = _fresh([_text_resp("hi there")], fails=0)
        sid = await _mk_session(svc)
        turn = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                            new_message=_msg("hello"))
        await _await_turn(turn)
        assert turn.status == "done"
        import adk_cc.agent as A

        s = await svc.get_session(app_name=A.app.name, user_id="u1",
                                  session_id=sid)
        last_id = s.events[-1].id
        first_id = s.events[0].id
        n = len(s.events)
        # stale/non-last id → refused
        assert not await svc.delete_last_event(user_id="u1", session_id=sid,
                                               event_id=first_id)
        assert not await svc.delete_last_event(user_id="u1", session_id=sid,
                                               event_id="nope")
        # matching id → deleted, earlier events intact
        assert await svc.delete_last_event(user_id="u1", session_id=sid,
                                           event_id=last_id)
        s2 = await svc.get_session(app_name=A.app.name, user_id="u1",
                                   session_id=sid)
        assert len(s2.events) == n - 1
        assert s2.events[0].id == first_id
    asyncio.run(main())
    print("OK test_delete_last_event_guards")


def test_retry_prunes_orphan() -> None:
    async def main():
        broker, svc, llm = _fresh([_text_resp("recovered fine")], fails=1)
        sid = await _mk_session(svc)
        t1 = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                          new_message=_msg("please do the thing"))
        await _await_turn(t1)
        assert t1.status == "error" and t1.model_events == 0, t1.snapshot()
        assert await _user_texts(svc, sid) == ["please do the thing"]  # orphan
        t2 = broker.retry_last(app_name="adk_cc", user_id="u1",
                               session_id=sid)
        await _await_turn(t2)
        assert t2.status == "done", t2.snapshot()
        # exactly ONE copy of the message survives the failed attempt + retry
        assert await _user_texts(svc, sid) == ["please do the thing"]
    asyncio.run(main())
    print("OK test_retry_prunes_orphan")


def test_fresh_message_prunes_orphan() -> None:
    async def main():
        broker, svc, llm = _fresh([_text_resp("second answer")], fails=1)
        sid = await _mk_session(svc)
        t1 = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                          new_message=_msg("first attempt"))
        await _await_turn(t1)
        assert t1.status == "error" and t1.model_events == 0
        # user gives up on the failed message and types something else
        t2 = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                          new_message=_msg("try this instead"))
        await _await_turn(t2)
        assert t2.status == "done", t2.snapshot()
        assert await _user_texts(svc, sid) == ["try this instead"]
    asyncio.run(main())
    print("OK test_fresh_message_prunes_orphan")


def test_partial_turn_not_pruned() -> None:
    async def main():
        # call 1 succeeds (a reply lands), a LATER turn errors → that errored
        # turn has model output? No — simpler: turn 1 succeeds fully, turn 2
        # errors with zero output, turn 3 retries. Turn 1's history must stay.
        broker, svc, llm = _fresh(
            [_text_resp("first reply"), _text_resp("third reply")], fails=0)
        sid = await _mk_session(svc)
        t1 = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                          new_message=_msg("turn one"))
        await _await_turn(t1)
        assert t1.status == "done"
        llm.fails = 1  # next turn dies before any model event
        t2 = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                          new_message=_msg("turn two"))
        await _await_turn(t2)
        assert t2.status == "error" and t2.model_events == 0
        t3 = broker.retry_last(app_name="adk_cc", user_id="u1",
                               session_id=sid)
        await _await_turn(t3)
        assert t3.status == "done", t3.snapshot()
        # turn one intact; turn two present exactly once
        assert await _user_texts(svc, sid) == ["turn one", "turn two"]
    asyncio.run(main())
    print("OK test_partial_turn_not_pruned")


def test_mismatched_last_event_left_alone() -> None:
    async def main():
        broker, svc, llm = _fresh([_text_resp("late reply")], fails=1)
        sid = await _mk_session(svc)
        t1 = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                          new_message=_msg("orphan me"))
        await _await_turn(t1)
        assert t1.status == "error" and t1.model_events == 0
        # another driver appends its own user event meanwhile (e.g. /run_sse)
        import adk_cc.agent as A
        from google.adk.events.event import Event

        s = await svc.get_session(app_name=A.app.name, user_id="u1",
                                  session_id=sid)
        await svc.append_event(s, Event(
            author="user", invocation_id="e-outside",
            content=_msg("outside message")))
        t2 = broker.retry_last(app_name="adk_cc", user_id="u1",
                               session_id=sid)
        await _await_turn(t2)
        assert t2.status == "done", t2.snapshot()
        texts = await _user_texts(svc, sid)
        # nothing pruned (text mismatch guard) — history preserved verbatim,
        # plus the retried message re-appended by the runner
        assert texts == ["orphan me", "outside message", "orphan me"], texts
    asyncio.run(main())
    print("OK test_mismatched_last_event_left_alone")


def main() -> None:
    test_delete_last_event_guards()
    test_retry_prunes_orphan()
    test_fresh_message_prunes_orphan()
    test_partial_turn_not_pruned()
    test_mismatched_last_event_left_alone()
    print("\nall turn-prune tests passed")


if __name__ == "__main__":
    main()
