"""Auth rate-limit primitives: sliding-window budget + failure lockout.
Pure in-memory tests (sub-second windows) — nothing executes.

Run: .venv/bin/python tests/test_ratelimit.py
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")

from adk_cc.identity.ratelimit import FailureLockout, SlidingWindowLimiter


def test_window_budget_and_recovery():
    lim = SlidingWindowLimiter(limit=3, window_s=0.2)
    assert [lim.allow("k") for _ in range(3)] == [True, True, True]
    assert lim.allow("k") is False  # over budget
    time.sleep(0.25)
    assert lim.allow("k") is True  # window slid


def test_window_keys_independent():
    lim = SlidingWindowLimiter(limit=1, window_s=10)
    assert lim.allow("a") is True
    assert lim.allow("a") is False
    assert lim.allow("b") is True  # unaffected


def test_lockout_after_threshold_and_expiry():
    lk = FailureLockout(threshold=2, lockout_s=0.3)
    assert lk.locked_for("k") == 0.0
    lk.record_failure("k")
    assert lk.locked_for("k") == 0.0  # below threshold
    lk.record_failure("k")
    assert lk.locked_for("k") > 0.0  # locked
    time.sleep(0.35)
    assert lk.locked_for("k") == 0.0  # aged out


def test_lockout_cleared_by_success():
    lk = FailureLockout(threshold=2, lockout_s=10)
    lk.record_failure("k")
    lk.record_failure("k")
    assert lk.locked_for("k") > 0.0
    lk.clear("k")
    assert lk.locked_for("k") == 0.0


def test_lockout_keys_independent():
    lk = FailureLockout(threshold=1, lockout_s=10)
    lk.record_failure("ip1|victim@x.io")
    assert lk.locked_for("ip1|victim@x.io") > 0.0
    assert lk.locked_for("ip2|victim@x.io") == 0.0  # other IP unaffected
    assert lk.locked_for("ip1|other@x.io") == 0.0  # other account unaffected


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
    print("\nall ratelimit tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
