"""Tests for the actionable task-not-found response.

A bare "task X not found for tenant Y" led models to conclude the
session was empty and abandon tracking. task_not_found_error instead
lists the session's real task ids so the model self-corrects.

Hand-rolled (no pytest), runnable with the venv python.
"""

from __future__ import annotations

import asyncio
import os
import types

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.tools.task._common import task_not_found_error


class _Status:
    def __init__(self, value):
        self.value = value


class _Task:
    def __init__(self, tid, status, title):
        self.id = tid
        self.status = _Status(status)
        self.title = title


class _Storage:
    def __init__(self, tasks):
        self._tasks = tasks
        self.queried = None

    async def list(self, *, tenant_id, session_id=None, workspace_path=None):
        self.queried = (tenant_id, session_id, workspace_path)
        return self._tasks


class _Runner:
    def __init__(self, tasks):
        self.storage = _Storage(tasks)


_WS = types.SimpleNamespace(
    tenant_id="acme", session_id="sess-1", abs_path="/work/acme/alice"
)


def _run(coro):
    return asyncio.run(coro)


def test_lists_real_ids_when_tasks_exist():
    runner = _Runner(
        [
            _Task("37eee8cd", "pending", "Create test_temperature.py"),
            _Task("a1ddebbd", "completed", "Create temperature.py"),
        ]
    )
    out = _run(task_not_found_error("b8aee5bb-phantom", runner, _WS))
    assert out["status"] == "not_found"
    # Must query the right bucket (workspace-anchored, real tenant/session).
    assert runner.storage.queried == ("acme", "sess-1", "/work/acme/alice")
    # Must surface the real ids so the model can retry with the right one.
    ids = {t["task_id"] for t in out["existing_tasks"]}
    assert ids == {"37eee8cd", "a1ddebbd"}, ids
    assert out["existing_tasks"][0]["title"] == "Create test_temperature.py"
    assert out["existing_tasks"][0]["status"] == "pending"
    # Message must counter the "session is empty / tracking lost" misread.
    assert "NOT empty" in out["error"]
    assert "b8aee5bb-phantom" in out["error"]
    print("OK test_lists_real_ids_when_tasks_exist")


def test_says_create_first_when_no_tasks():
    runner = _Runner([])
    out = _run(task_not_found_error("whatever", runner, _WS))
    assert out["status"] == "not_found"
    assert out["existing_tasks"] == []
    assert "task_create" in out["error"]
    assert "no tasks yet" in out["error"]
    print("OK test_says_create_first_when_no_tasks")


def test_storage_failure_degrades_gracefully():
    class _BoomStorage:
        async def list(self, **kw):
            raise RuntimeError("disk gone")

    runner = types.SimpleNamespace(storage=_BoomStorage())
    out = _run(task_not_found_error("x", runner, _WS))
    # No crash; falls back to the empty-session message.
    assert out["status"] == "not_found"
    assert out["existing_tasks"] == []
    print("OK test_storage_failure_degrades_gracefully")


if __name__ == "__main__":
    test_lists_real_ids_when_tasks_exist()
    test_says_create_first_when_no_tasks()
    test_storage_failure_degrades_gracefully()
    print("\nall task-not-found tests passed")
