"""SessionTitlePlugin must NOT hold the run open waiting for the out-of-band
title call — that's the ~5s phantom "agent is working…" tail after a reply.

Pins: after_run returns immediately when the title task is still running (title
persisted later, detached), persists INLINE when the task already finished, and
does nothing when no task was spawned.

Run: `.venv/bin/python tests/test_session_title_nonblocking.py`
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.plugins.session_title import _STATE_KEY, SessionTitlePlugin


class _FakeSvc:
    def __init__(self) -> None:
        self.appended: list[dict] = []

    async def append_event(self, session, event) -> None:
        delta = event.actions.state_delta
        self.appended.append(delta)
        # Mimic ADK: applying the delta to in-memory session state.
        (session.state or {}).update(delta)


def _ictx(svc: _FakeSvc, inv: str = "inv1"):
    return SimpleNamespace(
        session=SimpleNamespace(state={}, id="s1", events=[]),
        session_service=svc,
        invocation_id=inv,
    )


async def test_after_run_does_not_block_on_slow_title() -> None:
    plugin = SessionTitlePlugin()
    svc = _FakeSvc()
    ictx = _ictx(svc)

    async def _slow() -> str:
        await asyncio.sleep(1.0)
        return "My Slow Title"

    plugin._pending[ictx.invocation_id] = asyncio.create_task(_slow())

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await plugin.after_run_callback(invocation_context=ictx)
    elapsed = loop.time() - t0
    assert elapsed < 0.3, f"after_run blocked on the title call ({elapsed:.2f}s) — the SSE tail bug"
    assert svc.appended == [], "title should NOT be persisted synchronously for a slow call"

    # ...but it IS persisted a moment later, detached.
    await asyncio.sleep(1.2)
    assert svc.appended == [{_STATE_KEY: "My Slow Title"}], f"detached persist missing: {svc.appended}"
    print("OK test_after_run_does_not_block_on_slow_title")


async def test_after_run_persists_inline_when_ready() -> None:
    plugin = SessionTitlePlugin()
    svc = _FakeSvc()
    ictx = _ictx(svc, inv="inv2")

    async def _quick() -> str:
        return "Ready Title"

    task = asyncio.create_task(_quick())
    await asyncio.sleep(0.05)  # let it finish
    assert task.done()
    plugin._pending[ictx.invocation_id] = task

    await plugin.after_run_callback(invocation_context=ictx)
    assert svc.appended == [{_STATE_KEY: "Ready Title"}], "a finished title should persist inline"
    print("OK test_after_run_persists_inline_when_ready")


async def test_no_task_no_persist() -> None:
    plugin = SessionTitlePlugin()
    svc = _FakeSvc()
    await plugin.after_run_callback(invocation_context=_ictx(svc, inv="inv3"))
    assert svc.appended == [], "no spawned task → nothing persisted, returns immediately"
    print("OK test_no_task_no_persist")


async def _run_all() -> None:
    await test_after_run_does_not_block_on_slow_title()
    await test_after_run_persists_inline_when_ready()
    await test_no_task_no_persist()


def main() -> None:
    asyncio.run(_run_all())
    print("\nall session-title non-blocking tests passed")


if __name__ == "__main__":
    main()
