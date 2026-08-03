"""Parent watchdog (#98): the desktop backend must die with its app.

Verified live before the fix: SIGTERM/SIGKILL of the app orphaned the
uvicorn child, which held the port forever; the next launch adopted the
stale orphan and silently served old code. These tests pin the watchdog's
contract with injectable getppid/action — the real orphan action is
os._exit(0).

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_parent_watchdog.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.service import parent_watchdog as pw  # noqa: E402

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    # --- no env -> no watchdog -----------------------------------------
    pw._reset_for_test()
    os.environ.pop("ADK_CC_PARENT_PID", None)
    check("without ADK_CC_PARENT_PID nothing starts",
          pw.start_parent_watchdog() is False)

    # --- garbage env -> declined, not crashed ---------------------------
    os.environ["ADK_CC_PARENT_PID"] = "not-a-pid"
    check("a garbage pid declines gracefully",
          pw.start_parent_watchdog() is False)

    # --- parent alive -> silent; parent gone -> action fires ------------
    os.environ["ADK_CC_PARENT_PID"] = "4242"
    pw._reset_for_test()
    fired = threading.Event()
    current = {"ppid": 4242}
    started = pw.start_parent_watchdog(
        getppid=lambda: current["ppid"], interval_s=0.02,
        on_orphaned=fired.set)
    check("watchdog starts when the env is set", started is True)
    time.sleep(0.15)
    check("nothing fires while the parent lives", not fired.is_set())
    current["ppid"] = 1          # re-parented to launchd = parent died
    check("the orphan action fires once the parent dies",
          fired.wait(timeout=2.0))

    # --- idempotent per process -----------------------------------------
    check("a second start is a no-op",
          pw.start_parent_watchdog(getppid=lambda: 4242) is False)

    os.environ.pop("ADK_CC_PARENT_PID", None)
    pw._reset_for_test()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
