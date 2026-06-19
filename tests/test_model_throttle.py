"""Tests for the SelectableLlm global rate-limit throttle (models/selectable.py).

The throttle spaces model-call STARTS by a configured minimum (from
ADK_CC_MODEL_MAX_RPM or ADK_CC_MODEL_MIN_INTERVAL_S) so bursts can't trip a
shared rate cap. Default off. Hand-rolled.
"""

from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.models import selectable as S


def _reset():
    S._pace_lock = None
    S._pace_last_at = 0.0


def test_min_interval_resolution():
    for k in ("ADK_CC_MODEL_MAX_RPM", "ADK_CC_MODEL_MIN_INTERVAL_S"):
        os.environ.pop(k, None)
    assert S._model_min_interval() == 0.0  # off by default
    os.environ["ADK_CC_MODEL_MAX_RPM"] = "30"
    assert abs(S._model_min_interval() - 2.0) < 1e-9  # 60/30
    os.environ.pop("ADK_CC_MODEL_MAX_RPM", None)
    os.environ["ADK_CC_MODEL_MIN_INTERVAL_S"] = "1.5"
    assert abs(S._model_min_interval() - 1.5) < 1e-9
    os.environ.pop("ADK_CC_MODEL_MIN_INTERVAL_S", None)
    print("OK min_interval_resolution")


def test_paces_calls_when_enabled():
    os.environ["ADK_CC_MODEL_MIN_INTERVAL_S"] = "0.2"
    _reset()

    async def run():
        t = time.monotonic()
        for _ in range(4):  # 1st immediate, then 3 spaced by 0.2 → ≥0.6s
            await S._pace_model_call()
        return time.monotonic() - t

    elapsed = asyncio.run(run())
    assert elapsed >= 0.55, f"not paced: {elapsed:.3f}s"
    os.environ.pop("ADK_CC_MODEL_MIN_INTERVAL_S", None)
    print(f"OK paces_calls_when_enabled ({elapsed:.2f}s for 4 calls)")


def test_concurrent_calls_are_serialized_in_spacing():
    os.environ["ADK_CC_MODEL_MIN_INTERVAL_S"] = "0.2"
    _reset()

    async def run():
        t = time.monotonic()
        await asyncio.gather(*[S._pace_model_call() for _ in range(4)])
        return time.monotonic() - t

    elapsed = asyncio.run(run())
    assert elapsed >= 0.55, f"concurrent burst not paced: {elapsed:.3f}s"
    os.environ.pop("ADK_CC_MODEL_MIN_INTERVAL_S", None)
    print(f"OK concurrent_calls_are_serialized_in_spacing ({elapsed:.2f}s)")


def test_no_throttle_when_unset():
    for k in ("ADK_CC_MODEL_MAX_RPM", "ADK_CC_MODEL_MIN_INTERVAL_S"):
        os.environ.pop(k, None)
    _reset()

    async def run():
        t = time.monotonic()
        for _ in range(50):
            await S._pace_model_call()
        return time.monotonic() - t

    assert asyncio.run(run()) < 0.1, "throttle should be a no-op when unset"
    print("OK no_throttle_when_unset")


def main():
    test_min_interval_resolution()
    test_paces_calls_when_enabled()
    test_concurrent_calls_are_serialized_in_spacing()
    test_no_throttle_when_unset()
    print("\nall model-throttle tests passed")


if __name__ == "__main__":
    main()
