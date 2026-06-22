"""Phase 6 tests: compaction circuit breaker. Model-free."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.agent import _CompactionBreaker


def _save(*keys):
    return {k: os.environ.get(k) for k in keys}


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_opens_after_threshold_consecutive_failures():
    saved = _save("ADK_CC_COMPACTION_BREAKER_THRESHOLD", "ADK_CC_COMPACTION_BREAKER_COOLDOWN_S")
    os.environ["ADK_CC_COMPACTION_BREAKER_THRESHOLD"] = "3"
    os.environ["ADK_CC_COMPACTION_BREAKER_COOLDOWN_S"] = "60"
    try:
        b = _CompactionBreaker()
        assert not b.should_skip(now=100.0)
        b.record_failure(now=100.0)
        b.record_failure(now=101.0)
        assert not b.should_skip(now=102.0)   # 2 < 3, still closed
        b.record_failure(now=102.0)           # 3rd → opens until 162
        assert b.should_skip(now=103.0)       # within cooldown
        assert b.should_skip(now=161.9)
        assert not b.should_skip(now=162.1)   # cooldown elapsed
    finally:
        _restore(saved)


def test_success_resets_failure_count():
    saved = _save("ADK_CC_COMPACTION_BREAKER_THRESHOLD", "ADK_CC_COMPACTION_BREAKER_COOLDOWN_S")
    os.environ["ADK_CC_COMPACTION_BREAKER_THRESHOLD"] = "2"
    os.environ["ADK_CC_COMPACTION_BREAKER_COOLDOWN_S"] = "30"
    try:
        b = _CompactionBreaker()
        b.record_failure(now=10.0)
        b.record_success()                    # resets
        b.record_failure(now=11.0)            # count back to 1, not 2
        assert not b.should_skip(now=12.0)
        b.record_failure(now=12.0)            # now 2 → opens
        assert b.should_skip(now=13.0)
    finally:
        _restore(saved)


def test_disabled_when_threshold_zero():
    saved = _save("ADK_CC_COMPACTION_BREAKER_THRESHOLD")
    os.environ["ADK_CC_COMPACTION_BREAKER_THRESHOLD"] = "0"
    try:
        b = _CompactionBreaker()
        for i in range(10):
            b.record_failure(now=float(i))
        assert not b.should_skip(now=100.0), "threshold 0 → breaker never opens"
    finally:
        _restore(saved)


def test_default_threshold_is_three():
    saved = _save("ADK_CC_COMPACTION_BREAKER_THRESHOLD", "ADK_CC_COMPACTION_BREAKER_COOLDOWN_S")
    os.environ.pop("ADK_CC_COMPACTION_BREAKER_THRESHOLD", None)
    os.environ.pop("ADK_CC_COMPACTION_BREAKER_COOLDOWN_S", None)
    try:
        b = _CompactionBreaker()
        b.record_failure(now=0.0)
        b.record_failure(now=0.0)
        assert not b.should_skip(now=0.0)  # 2 < default 3
        b.record_failure(now=0.0)
        assert b.should_skip(now=0.0)      # 3 → open (default cooldown 60)
        assert not b.should_skip(now=61.0)
    finally:
        _restore(saved)


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__[5:]}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__[5:]}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__[5:]}: {type(e).__name__}: {e}")
    print("\nall compaction-breaker tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
