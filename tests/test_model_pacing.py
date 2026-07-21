"""Regression tests for the model-call pacing throttle (`_pace_model_call`).

The field failure (desktop, 2026-07-21): pacing used a module-global
`asyncio.Lock`, which BINDS to the first event loop that acquires it under
contention. adk-cc paces model calls from uvicorn's loop (chat turns) AND
from throwaway `asyncio.run()` loops (memory synth/resolve/canonicalize) —
so once the lock bound to one side, the other side died with
`RuntimeError: ... is bound to a different event loop`, permanently, until
restart. The repro below is that exact shape: contended pacing in one loop,
then contended pacing in a second loop.

The fix reserves start-slots under a threading.Lock (loop-agnostic, held
for arithmetic only) and sleeps outside it — global spacing across all
loops and threads, no cross-loop asyncio primitive.

Run: `uv run python tests/test_model_pacing.py`
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.models import selectable as sel  # noqa: E402

_INTERVAL_VAR = "ADK_CC_MODEL_MIN_INTERVAL_S"


def _reset(interval: str | None) -> None:
    """Fresh throttle state per test; works on both pre- and post-fix code."""
    sel._pace_last_at = 0.0
    if hasattr(sel, "_pace_lock"):  # pre-fix global asyncio.Lock
        sel._pace_lock = None
    if interval is None:
        os.environ.pop(_INTERVAL_VAR, None)
    else:
        os.environ[_INTERVAL_VAR] = interval
    os.environ.pop("ADK_CC_MODEL_MAX_RPM", None)


def _pace_contended_once() -> None:
    """One fresh event loop with two pacers in REAL contention: priming
    `_pace_last_at` to 'now' forces the first pacer to sleep ~interval while
    holding the (pre-fix) lock, so the second pacer takes the contended
    acquire path — which is what binds an asyncio.Lock to a loop. Bounded so
    a pre-fix cross-loop deadlock fails the test instead of hanging it."""
    sel._pace_last_at = time.monotonic()

    async def two() -> None:
        await asyncio.wait_for(
            asyncio.gather(sel._pace_model_call(), sel._pace_model_call()),
            timeout=10,
        )

    asyncio.run(two())


def test_pacing_survives_a_second_event_loop():
    """THE field failure: contended pacing in loop A, then contended pacing
    in loop B. Pre-fix: loop B raises RuntimeError('bound to a different
    event loop'). Post-fix: both succeed."""
    _reset("0.05")
    _pace_contended_once()  # loop A — binds the (pre-fix) lock
    _pace_contended_once()  # loop B — pre-fix: RuntimeError
    print("OK pacing_survives_a_second_event_loop")


def test_pacing_survives_thread_loop_interleaving():
    """Same failure via the exact field topology: the 'main' loop paces
    while a worker thread paces in its own asyncio.run() loop (memory
    synth's pattern), concurrently."""
    _reset("0.05")
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            _pace_contended_once()
        except BaseException as e:  # noqa: BLE001 — recorded for the assert
            errors.append(e)

    async def main_side() -> None:
        t = threading.Thread(target=worker)
        t.start()
        # Interleave with the worker's loop on purpose.
        await asyncio.gather(sel._pace_model_call(), sel._pace_model_call())
        t.join()

    asyncio.run(main_side())
    assert not errors, f"worker loop died: {errors[0]!r}"
    print("OK pacing_survives_thread_loop_interleaving")


def test_spacing_holds_same_loop():
    _reset("0.15")

    async def three() -> None:
        for _ in range(3):
            await sel._pace_model_call()

    t0 = time.monotonic()
    asyncio.run(three())
    wall = time.monotonic() - t0
    # First call is free (last_at=0), the next two wait ~0.15 each.
    assert wall >= 0.28, f"expected >=0.28s of pacing, got {wall:.3f}s"
    print("OK spacing_holds_same_loop")


def test_spacing_holds_across_loops_and_threads():
    """Pacing is GLOBAL: a call from a fresh thread-loop respects the slot
    taken by the main loop's call."""
    _reset("0.2")
    asyncio.run(sel._pace_model_call())
    first = sel._pace_last_at
    t = threading.Thread(target=lambda: asyncio.run(sel._pace_model_call()))
    t0 = time.monotonic()
    t.start()
    t.join()
    wall = time.monotonic() - t0
    assert sel._pace_last_at - first >= 0.19, (
        f"slots not spaced: {sel._pace_last_at - first:.3f}s"
    )
    assert wall >= 0.15, f"second caller didn't actually wait ({wall:.3f}s)"
    print("OK spacing_holds_across_loops_and_threads")


def test_concurrent_burst_is_serialized():
    _reset("0.1")

    async def burst() -> None:
        await asyncio.gather(*[sel._pace_model_call() for _ in range(4)])

    t0 = time.monotonic()
    asyncio.run(burst())
    wall = time.monotonic() - t0
    # 4 callers → 3 inter-call gaps of ≥0.1s each.
    assert wall >= 0.28, f"burst not paced: {wall:.3f}s"
    print("OK concurrent_burst_is_serialized")


def test_disabled_is_free():
    _reset(None)

    async def many() -> None:
        for _ in range(50):
            await sel._pace_model_call()

    t0 = time.monotonic()
    asyncio.run(many())
    assert time.monotonic() - t0 < 0.05, "disabled throttle must be a no-op"
    print("OK disabled_is_free")


def main() -> None:
    test_pacing_survives_a_second_event_loop()
    test_pacing_survives_thread_loop_interleaving()
    test_spacing_holds_same_loop()
    test_spacing_holds_across_loops_and_threads()
    test_concurrent_burst_is_serialized()
    test_disabled_is_free()
    print("\nall model-pacing tests passed")


if __name__ == "__main__":
    main()
