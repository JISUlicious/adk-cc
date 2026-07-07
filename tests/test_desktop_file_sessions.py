"""FileSessionService: per-project JSONL session store for desktop mode.

Pins the ADK BaseSessionService contract + the state-scoping that makes it
correct: session-scoped state in the per-session file, user:/app: state in shared
side-files (so a project's allow-rules survive across its sessions), temp: never
persisted, and durability across service instances (the whole point).

Run: `.venv/bin/python tests/test_desktop_file_sessions.py`
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.sessions.base_session_service import GetSessionConfig
from google.genai import types

from adk_cc.service.file_session_service import FileSessionService

APP = "adk_cc"


def _ev(state_delta=None, ts=None, author="agent") -> Event:
    kwargs = {"author": author, "actions": EventActions(state_delta=state_delta or {})}
    if ts is not None:
        kwargs["timestamp"] = ts
    return Event(**kwargs)


async def test_create_get_roundtrip_and_layout(base: str) -> None:
    svc = FileSessionService(base)
    s = await svc.create_session(app_name=APP, user_id="projA", state={"k": "v"}, session_id="s1")
    assert s.id == "s1" and s.state["k"] == "v"
    # Layout: projects/<user_id>/sessions/<id>.jsonl
    f = Path(base) / "projects" / "projA" / "sessions" / "s1.jsonl"
    assert f.is_file(), f"session file missing at {f}"
    got = await svc.get_session(app_name=APP, user_id="projA", session_id="s1")
    assert got is not None and got.id == "s1" and got.state["k"] == "v"
    print("OK test_create_get_roundtrip_and_layout")


async def test_events_persist_in_order(base: str) -> None:
    svc = FileSessionService(base)
    s = await svc.create_session(app_name=APP, user_id="projB", state={}, session_id="s1")
    await svc.append_event(s, _ev({"step": 1}, ts=100.0))
    await svc.append_event(s, _ev({"step": 2}, ts=200.0))
    got = await svc.get_session(app_name=APP, user_id="projB", session_id="s1")
    assert [e.actions.state_delta.get("step") for e in got.events] == [1, 2], "event order/content wrong"
    assert got.state["step"] == 2, "session state should reflect the latest delta"
    assert got.last_update_time == 200.0, f"last_update_time={got.last_update_time}"
    print("OK test_events_persist_in_order")


async def test_user_state_shared_across_sessions(base: str) -> None:
    # THE correctness case: user:adk_cc_allow_rules-style state set in one session
    # must be visible in every other session of the same project (user_id).
    svc = FileSessionService(base)
    s1 = await svc.create_session(app_name=APP, user_id="projC", state={}, session_id="s1")
    await svc.append_event(s1, _ev({"user:allow_rules": ["rule-x"], "sess_only": 7}))
    s2 = await svc.create_session(app_name=APP, user_id="projC", state={}, session_id="s2")
    got2 = await svc.get_session(app_name=APP, user_id="projC", session_id="s2")
    assert got2.state.get("user:allow_rules") == ["rule-x"], "user: state didn't cross sessions"
    assert "sess_only" not in got2.state, "session-scoped state leaked across sessions"
    print("OK test_user_state_shared_across_sessions")


async def test_app_state_shared_across_users(base: str) -> None:
    svc = FileSessionService(base)
    s = await svc.create_session(app_name=APP, user_id="projD", state={}, session_id="s1")
    await svc.append_event(s, _ev({"app:global_flag": "on"}))
    # A session under a DIFFERENT project/user sees app: state.
    await svc.create_session(app_name=APP, user_id="projE", state={}, session_id="s1")
    got = await svc.get_session(app_name=APP, user_id="projE", session_id="s1")
    assert got.state.get("app:global_flag") == "on", "app: state not shared across users"
    print("OK test_app_state_shared_across_users")


async def test_temp_state_not_persisted(base: str) -> None:
    svc = FileSessionService(base)
    s = await svc.create_session(app_name=APP, user_id="projF", state={}, session_id="s1")
    await svc.append_event(s, _ev({"temp:scratch": 1, "keep": 2}))
    got = await svc.get_session(app_name=APP, user_id="projF", session_id="s1")
    assert got.state.get("keep") == 2
    assert "temp:scratch" not in got.state, "temp: state must not persist"
    raw = (Path(base) / "projects" / "projF" / "sessions" / "s1.jsonl").read_text()
    assert "temp:scratch" not in raw, "temp: leaked into the file"
    print("OK test_temp_state_not_persisted")


async def test_get_session_config_filters_events(base: str) -> None:
    svc = FileSessionService(base)
    s = await svc.create_session(app_name=APP, user_id="projG", state={}, session_id="s1")
    for i, ts in enumerate([10.0, 20.0, 30.0], start=1):
        await svc.append_event(s, _ev({"n": i}, ts=ts))
    recent = await svc.get_session(
        app_name=APP, user_id="projG", session_id="s1", config=GetSessionConfig(num_recent_events=1)
    )
    assert len(recent.events) == 1 and recent.events[0].actions.state_delta["n"] == 3
    assert recent.state["n"] == 3, "state must reflect FULL history even when events are filtered"
    # after_timestamp keeps events with ts >= the bound (ADK's inclusive semantics).
    after = await svc.get_session(
        app_name=APP, user_id="projG", session_id="s1", config=GetSessionConfig(after_timestamp=25.0)
    )
    assert [e.timestamp for e in after.events] == [30.0], f"after_timestamp wrong: {[e.timestamp for e in after.events]}"
    after2 = await svc.get_session(
        app_name=APP, user_id="projG", session_id="s1", config=GetSessionConfig(after_timestamp=20.0)
    )
    assert [e.timestamp for e in after2.events] == [20.0, 30.0], "after_timestamp is an inclusive lower bound"
    print("OK test_get_session_config_filters_events")


async def test_list_and_delete(base: str) -> None:
    svc = FileSessionService(base)
    await svc.create_session(app_name=APP, user_id="projH", state={}, session_id="s1")
    s2 = await svc.create_session(app_name=APP, user_id="projH", state={}, session_id="s2")
    await svc.append_event(s2, _ev({"user:pref": "dark"}))
    lst = await svc.list_sessions(app_name=APP, user_id="projH")
    ids = {s.id for s in lst.sessions}
    assert ids == {"s1", "s2"}, ids
    assert all(s.events == [] for s in lst.sessions), "list must not return events"
    assert all(s.state.get("user:pref") == "dark" for s in lst.sessions), "list must merge user state"
    # user_id=None lists across all projects.
    all_ = await svc.list_sessions(app_name=APP)
    assert {s.id for s in all_.sessions} >= {"s1", "s2"}
    # delete
    await svc.delete_session(app_name=APP, user_id="projH", session_id="s1")
    assert await svc.get_session(app_name=APP, user_id="projH", session_id="s1") is None
    assert not (Path(base) / "projects" / "projH" / "sessions" / "s1.jsonl").exists()
    print("OK test_list_and_delete")


async def test_durability_across_instances(base: str) -> None:
    # A fresh service on the same base dir sees everything — the point of a file
    # store (survives process restart).
    a = FileSessionService(base)
    s = await a.create_session(app_name=APP, user_id="projI", state={"init": 1}, session_id="s1")
    await a.append_event(s, _ev({"user:token": "abc", "body": "hi"}, ts=42.0))
    b = FileSessionService(base)  # simulate a restart
    got = await b.get_session(app_name=APP, user_id="projI", session_id="s1")
    assert got is not None
    assert got.state["init"] == 1 and got.state["body"] == "hi"
    assert got.state.get("user:token") == "abc"
    assert len(got.events) == 1 and got.last_update_time == 42.0
    print("OK test_durability_across_instances")


async def test_already_exists_and_path_safety(base: str) -> None:
    svc = FileSessionService(base)
    await svc.create_session(app_name=APP, user_id="projJ", state={}, session_id="dup")
    try:
        await svc.create_session(app_name=APP, user_id="projJ", state={}, session_id="dup")
    except AlreadyExistsError:
        pass
    else:
        raise AssertionError("re-creating an existing session id must raise AlreadyExistsError")
    # Path-escape ids are rejected before touching the filesystem.
    for bad in ("../evil", "a/b", "a b"):
        try:
            await svc.get_session(app_name=APP, user_id=bad, session_id="s1")
        except ValueError:
            continue
        raise AssertionError(f"unsafe id {bad!r} should raise ValueError")
    print("OK test_already_exists_and_path_safety")


async def test_truncate_before_invocation(base: str) -> None:
    # Rewind the conversation: drop all events from a given invocation onward.
    svc = FileSessionService(base)
    s = await svc.create_session(app_name=APP, user_id="projT", state={}, session_id="s1")
    await svc.append_event(s, Event(author="user", actions=EventActions(state_delta={}), invocation_id="inv-1", timestamp=1.0))
    await svc.append_event(s, Event(author="agent", actions=EventActions(state_delta={"a": 1}), invocation_id="inv-1", timestamp=2.0))
    await svc.append_event(s, Event(author="user", actions=EventActions(state_delta={}), invocation_id="inv-2", timestamp=3.0))
    await svc.append_event(s, Event(author="agent", actions=EventActions(state_delta={"b": 2}), invocation_id="inv-2", timestamp=4.0))

    kept = await svc.truncate_before_invocation(user_id="projT", session_id="s1", invocation_id="inv-2")
    assert kept == 2, kept
    got = await svc.get_session(app_name=APP, user_id="projT", session_id="s1")
    assert len(got.events) == 2 and all(e.invocation_id == "inv-1" for e in got.events), got.events
    assert got.state.get("a") == 1 and "b" not in got.state, "inv-2's session state should be gone"

    # Unknown invocation → no-op (returns current count, changes nothing).
    kept2 = await svc.truncate_before_invocation(user_id="projT", session_id="s1", invocation_id="nope")
    assert kept2 == 2, kept2
    # Truncating the FIRST invocation empties the conversation.
    kept3 = await svc.truncate_before_invocation(user_id="projT", session_id="s1", invocation_id="inv-1")
    assert kept3 == 0, kept3
    assert len((await svc.get_session(app_name=APP, user_id="projT", session_id="s1")).events) == 0
    print("OK test_truncate_before_invocation")


def _user_text(text: str, inv: str, ts: float) -> Event:
    return Event(author="user", content=types.Content(role="user", parts=[types.Part(text=text)]), invocation_id=inv, timestamp=ts)


def _agent_text(text: str, inv: str, ts: float) -> Event:
    return Event(author="coordinator", content=types.Content(role="model", parts=[types.Part(text=text)]), invocation_id=inv, timestamp=ts)


async def test_truncate_hitl_turn(base: str) -> None:
    # A turn that pauses for a HITL answer spans two invocations (request+ask, then
    # answer+act). Rewinding the edit (tagged with the ANSWER invocation) must roll
    # the whole logical turn back to the user's request — not stop at the answer.
    svc = FileSessionService(base)
    s = await svc.create_session(app_name=APP, user_id="projH", state={}, session_id="s1")
    await svc.append_event(s, _user_text("hi", "inv-1", 1.0))       # turn 1
    await svc.append_event(s, _agent_text("hello", "inv-1", 2.0))
    await svc.append_event(s, _user_text("edit the file", "inv-A", 3.0))  # turn 2: request
    await svc.append_event(s, Event(  # agent asks (long-running tool) — invocation A
        author="coordinator",
        content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="ask_user_question", args={}))]),
        invocation_id="inv-A", timestamp=4.0,
    ))
    await svc.append_event(s, Event(  # user's ANSWER arrives as a NEW invocation B
        author="user",
        content=types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(name="ask_user_question", response={}))]),
        invocation_id="inv-B", timestamp=5.0,
    ))
    await svc.append_event(s, _agent_text("done", "inv-B", 6.0))    # the edit happened here (inv-B)

    kept = await svc.truncate_before_invocation(user_id="projH", session_id="s1", invocation_id="inv-B")
    assert kept == 2, kept  # only turn 1 survives — the whole ask→answer→act turn is gone
    got = await svc.get_session(app_name=APP, user_id="projH", session_id="s1")
    texts = [p.text for e in got.events for p in (e.content.parts or []) if getattr(p, "text", None)]
    assert texts == ["hi", "hello"], texts
    print("OK test_truncate_hitl_turn")


async def _run_all() -> None:
    tests = [
        test_truncate_before_invocation,
        test_truncate_hitl_turn,
        test_create_get_roundtrip_and_layout,
        test_events_persist_in_order,
        test_user_state_shared_across_sessions,
        test_app_state_shared_across_users,
        test_temp_state_not_persisted,
        test_get_session_config_filters_events,
        test_list_and_delete,
        test_durability_across_instances,
        test_already_exists_and_path_safety,
    ]
    for t in tests:
        with tempfile.TemporaryDirectory(prefix="adk-cc-fss-") as base:
            await t(base)


def main() -> None:
    asyncio.run(_run_all())
    print("\nall FileSessionService tests passed")


if __name__ == "__main__":
    main()
