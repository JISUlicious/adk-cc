"""Unit tests for TaskReminderPlugin.

Covers the three behaviors changed in fix/task-tracking:
  - master on/off toggle (ADK_CC_TASK_REMINDER)
  - bucket resolution: queries the SAME (tenant, session) the task tools
    wrote to, via get_workspace (was: always local/local dict bug)
  - completion-aware cadence: an open in_progress task fires the reminder
    after OPEN_TURNS instead of TURNS_SINCE_WRITE

Hand-rolled (no pytest), runnable with the venv python like the other
suites in this dir.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

import adk_cc.plugins.task_reminder as tr
from adk_cc.plugins.task_reminder import TaskReminderPlugin
from adk_cc.sandbox.workspace import WorkspaceRoot
from adk_cc.tasks import TaskStatus


# --- fakes ----------------------------------------------------------------

class _FakeStatus:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeTask:
    def __init__(self, tid: str, status, title: str) -> None:
        self.id = tid
        self.status = status
        self.title = title


class _FakeStorage:
    """Records the (tenant_id, session_id, workspace_path) it was queried with."""

    def __init__(self, tasks):
        self._tasks = tasks
        self.queried_with = None
        self.queried_workspace_path = "<unset>"

    async def list(self, *, tenant_id, session_id, workspace_path=None):
        self.queried_with = (tenant_id, session_id)
        self.queried_workspace_path = workspace_path
        return self._tasks


class _FakeRunner:
    def __init__(self, storage):
        self.storage = storage


class _State(dict):
    """dict with attribute-free .get already provided by dict."""


class _Session:
    def __init__(self, events):
        self.events = events


class _Ctx:
    def __init__(self, *, state, events, agent_name=None, invocation_id="inv-1"):
        self.state = state
        self.session = _Session(events)
        self.agent_name = agent_name
        self.invocation_id = invocation_id


class _LlmReq:
    def __init__(self):
        self.config = types.SimpleNamespace(system_instruction=None)


def _plain_event(author="coordinator"):
    """An assistant event that counts as a turn (no task call, not thinking)."""
    content = types.SimpleNamespace(parts=[types.SimpleNamespace(text="x")])
    return types.SimpleNamespace(
        author=author, content=content, invocation_id="inv-1"
    )


def _task_call_event(name="task_create"):
    fc = types.SimpleNamespace(name=name)
    content = types.SimpleNamespace(parts=[types.SimpleNamespace(function_call=fc)])
    return types.SimpleNamespace(
        author="coordinator", content=content, invocation_id="inv-0"
    )


def _user_event():
    content = types.SimpleNamespace(parts=[types.SimpleNamespace(text="next request")])
    return types.SimpleNamespace(author="user", content=content, invocation_id="inv-2")


def _run(plugin, ctx, req):
    return asyncio.run(
        plugin.before_model_callback(callback_context=ctx, llm_request=req)
    )


def _install_runner(monkeypatch_tasks):
    """Point the plugin's get_runner at our fake. Returns the storage."""
    storage = _FakeStorage(monkeypatch_tasks)
    tr.get_runner = lambda: _FakeRunner(storage)  # type: ignore[assignment]
    return storage


# --- tests ----------------------------------------------------------------

def test_disabled_toggle_noops():
    os.environ["ADK_CC_TASK_REMINDER"] = "0"
    try:
        p = TaskReminderPlugin()
        ctx = _Ctx(
            state=_State(),
            events=[_plain_event() for _ in range(20)],
        )
        req = _LlmReq()
        _run(p, ctx, req)
        assert req.config.system_instruction is None, "disabled plugin must inject nothing"
    finally:
        os.environ["ADK_CC_TASK_REMINDER"] = "1"
    print("OK test_disabled_toggle_noops")


def test_bucket_uses_workspace_not_local():
    p = TaskReminderPlugin()
    ws = WorkspaceRoot(
        tenant_id="acme", session_id="sess-123", abs_path="/work/acme/alice"
    )
    storage = _install_runner([])
    state = _State({"temp:sandbox_workspace": ws})
    # No prior task call → turns_since = maxsize → fires; we only care
    # which bucket it queried.
    ctx = _Ctx(state=state, events=[_plain_event() for _ in range(5)])
    _run(p, ctx, _LlmReq())
    assert storage.queried_with == ("acme", "sess-123"), (
        f"expected real bucket, got {storage.queried_with}"
    )
    # Must forward workspace_path — without it, JsonFileTaskStorage.list
    # reads the legacy ~/.adk-cc/tasks root and finds nothing in any
    # workspace-anchored deployment (the bug that made the reminder inert).
    assert storage.queried_workspace_path == "/work/acme/alice", (
        f"reminder must pass workspace_path=ws.abs_path, "
        f"got {storage.queried_workspace_path!r}"
    )
    print("OK test_bucket_uses_workspace_not_local")


def test_completion_aware_fires_for_in_progress():
    # turns_since = 5: between OPEN_TURNS(3) and TURNS_SINCE_WRITE(10).
    tasks = [_FakeTask("abcd1234", TaskStatus.IN_PROGRESS, "Refactor login")]
    storage = _install_runner(tasks)
    p = TaskReminderPlugin()  # defaults: open=3, since_write=10, between=10
    ws = WorkspaceRoot(tenant_id="local", session_id="local", abs_path="/tmp")
    state = _State({"temp:sandbox_workspace": ws})
    events = [_task_call_event()] + [_plain_event() for _ in range(5)]
    req = _LlmReq()
    _run(p, ctx := _Ctx(state=state, events=events), req)
    assert req.config.system_instruction is not None, "in_progress should fire at 5 turns"
    assert "mark each finished task" in req.config.system_instruction
    assert "abcd1234" in req.config.system_instruction
    print("OK test_completion_aware_fires_for_in_progress")


def test_no_in_progress_holds_to_since_write():
    # Same 5-turn gap, but no in_progress → threshold is 10 → no fire.
    tasks = [_FakeTask("efgh5678", TaskStatus.PENDING, "Add tests")]
    _install_runner(tasks)
    p = TaskReminderPlugin()
    ws = WorkspaceRoot(tenant_id="local", session_id="local", abs_path="/tmp")
    state = _State({"temp:sandbox_workspace": ws})
    events = [_task_call_event()] + [_plain_event() for _ in range(5)]
    req = _LlmReq()
    _run(p, _Ctx(state=state, events=events), req)
    assert req.config.system_instruction is None, (
        "pending-only at 5 turns must wait for TURNS_SINCE_WRITE"
    )
    print("OK test_no_in_progress_holds_to_since_write")


def test_fresh_turn_with_open_tasks_fires_immediately():
    # New turn opens (events[-1] is a user msg) with an open task, but
    # turns_since (2) is below open_turns(3) and there's no cooldown
    # window yet — the dangling-task catch must fire anyway.
    tasks = [_FakeTask("dddd0001", TaskStatus.IN_PROGRESS, "Write tests")]
    _install_runner(tasks)
    p = TaskReminderPlugin()
    ws = WorkspaceRoot(tenant_id="acme", session_id="s2", abs_path="/tmp")
    state = _State({"temp:sandbox_workspace": ws})
    events = [_task_call_event(), _plain_event(), _plain_event(), _user_event()]
    req = _LlmReq()
    _run(p, _Ctx(state=state, events=events), req)
    assert req.config.system_instruction is not None, (
        "fresh turn with open tasks must fire even inside the cadence window"
    )
    assert "dddd0001" in req.config.system_instruction
    print("OK test_fresh_turn_with_open_tasks_fires_immediately")


def test_fresh_turn_no_open_tasks_does_not_fire():
    # Fresh turn but everything completed → nothing to confront, and
    # turns_since is small → no periodic fire either.
    tasks = [_FakeTask("eeee0001", TaskStatus.COMPLETED, "done thing")]
    _install_runner(tasks)
    p = TaskReminderPlugin()
    ws = WorkspaceRoot(tenant_id="acme", session_id="s3", abs_path="/tmp")
    state = _State({"temp:sandbox_workspace": ws})
    events = [_task_call_event(), _plain_event(), _user_event()]
    req = _LlmReq()
    _run(p, _Ctx(state=state, events=events), req)
    assert req.config.system_instruction is None, (
        "fresh turn with only completed tasks should not fire the dangling catch"
    )
    print("OK test_fresh_turn_no_open_tasks_does_not_fire")


def test_specialist_skipped():
    _install_runner([_FakeTask("x", TaskStatus.IN_PROGRESS, "t")])
    p = TaskReminderPlugin()
    ctx = _Ctx(
        state=_State(),
        events=[_plain_event() for _ in range(20)],
        agent_name="verification",
    )
    req = _LlmReq()
    _run(p, ctx, req)
    assert req.config.system_instruction is None, "specialists never get the reminder"
    print("OK test_specialist_skipped")


if __name__ == "__main__":
    test_disabled_toggle_noops()
    test_bucket_uses_workspace_not_local()
    test_completion_aware_fires_for_in_progress()
    test_no_in_progress_holds_to_since_write()
    test_fresh_turn_with_open_tasks_fires_immediately()
    test_fresh_turn_no_open_tasks_does_not_fire()
    test_specialist_skipped()
    print("\nall task-reminder tests passed")
