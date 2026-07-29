"""Deleting a session while its run is in flight.

Reported from dogfooding: deleting a session during agent work did not stop the
streaming, and the session came back in the list.

Both halves were real. `delete_session` unlinked the JSONL and nothing else —
no run was aborted, so the broker kept driving; and `append_event` re-creates
the file it writes to (`mkdir(parents=True)` + `open("a")`), so the very next
event wrote the session back into existence.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_delete_session_midrun.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from google.adk.events.event import Event  # noqa: E402
from google.genai import types  # noqa: E402

from adk_cc.service.file_session_service import FileSessionService  # noqa: E402
from adk_cc.service.turns import TurnBroker  # noqa: E402


def _event(text: str) -> Event:
    return Event(author="coordinator",
                 content=types.Content(role="model", parts=[types.Part(text=text)]))


def test_a_late_event_cannot_resurrect_the_session() -> None:
    """The reappearance, reduced: delete, then let the in-flight run append."""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as d:
            svc = FileSessionService(d)
            s = await svc.create_session(app_name="adk_cc", user_id="p1",
                                         session_id="s1")
            await svc.append_event(session=s, event=_event("before delete"))
            listed = await svc.list_sessions(app_name="adk_cc", user_id="p1")
            assert len(listed.sessions) == 1, listed.sessions

            await svc.delete_session(app_name="adk_cc", user_id="p1",
                                     session_id="s1")
            # The run has not noticed yet and flushes one more event.
            await svc.append_event(session=s, event=_event("after delete"))

            listed = await svc.list_sessions(app_name="adk_cc", user_id="p1")
            assert not listed.sessions, f"session came back: {listed.sessions}"
            got = await svc.get_session(app_name="adk_cc", user_id="p1",
                                        session_id="s1")
            assert got is None, "deleted session is readable again"

    asyncio.run(run())
    print("OK a_late_event_cannot_resurrect_the_session")


def test_recreating_the_same_id_still_works() -> None:
    """A tombstone must not brick the id — the UI reuses ids on 'new chat'
    in some flows, and a delete followed by a create is legitimate."""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as d:
            svc = FileSessionService(d)
            await svc.create_session(app_name="adk_cc", user_id="p1",
                                     session_id="s1")
            await svc.delete_session(app_name="adk_cc", user_id="p1",
                                     session_id="s1")
            s2 = await svc.create_session(app_name="adk_cc", user_id="p1",
                                          session_id="s1")
            await svc.append_event(session=s2, event=_event("fresh"))
            got = await svc.get_session(app_name="adk_cc", user_id="p1",
                                        session_id="s1")
            assert got is not None and len(got.events) == 1, got

    asyncio.run(run())
    print("OK recreating_the_same_id_still_works")


def test_deleting_one_session_leaves_its_siblings_alone() -> None:
    """The tombstone is per (project, session), not per project."""

    async def run() -> None:
        with tempfile.TemporaryDirectory() as d:
            svc = FileSessionService(d)
            a = await svc.create_session(app_name="adk_cc", user_id="p1",
                                         session_id="sa")
            b = await svc.create_session(app_name="adk_cc", user_id="p1",
                                         session_id="sb")
            await svc.delete_session(app_name="adk_cc", user_id="p1",
                                     session_id="sa")
            await svc.append_event(session=b, event=_event("still fine"))
            listed = await svc.list_sessions(app_name="adk_cc", user_id="p1")
            assert [x.id for x in listed.sessions] == ["sb"], listed.sessions
            assert a.id == "sa"

    asyncio.run(run())
    print("OK deleting_one_session_leaves_its_siblings_alone")


def test_abort_session_cancels_running_turns_only() -> None:
    """Deletion must stop the work. `abort_session` cancels every running turn
    for that session — plural, because a confirmation retry can leave an
    earlier turn running and only the latest is indexed by session."""

    async def run() -> None:
        broker = TurnBroker(get_runner=None, session_service=None)

        async def _forever() -> None:
            await asyncio.Event().wait()

        from adk_cc.service.turns import Turn

        made = []
        for sid, status in (("s1", "running"), ("s1", "running"),
                            ("s1", "done"), ("s2", "running")):
            t = Turn(app_name="adk_cc", user_id="p1", session_id=sid,
                     new_message=None)
            t.status = status
            t.task = asyncio.ensure_future(_forever())
            broker._turns[t.id] = t
            made.append(t)

        stopped = await broker.abort_session("adk_cc", "p1", "s1")
        await asyncio.sleep(0)
        assert stopped == 2, stopped
        assert made[0].task.cancelled() or made[0].task.cancelling()
        assert not made[3].task.cancelled(), "other session's turn was cancelled"
        for t in made:
            t.task.cancel()

    asyncio.run(run())
    print("OK abort_session_cancels_running_turns_only")


def main() -> None:
    test_a_late_event_cannot_resurrect_the_session()
    test_recreating_the_same_id_still_works()
    test_deleting_one_session_leaves_its_siblings_alone()
    test_abort_session_cancels_running_turns_only()
    print("\nall delete-mid-run tests passed")


if __name__ == "__main__":
    main()
