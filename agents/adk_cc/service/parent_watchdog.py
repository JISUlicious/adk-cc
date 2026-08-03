"""Parent watchdog: the desktop backend must die with its app.

Verified live (#98): Tauri's exit cleanup only runs on a graceful quit —
SIGTERM/SIGKILL of the app orphaned the uvicorn child, which then held the
port forever; the next launch's fresh child died on bind failure and the
splash-wait adopted the STALE orphan, silently serving old code.

The app passes its own pid as ADK_CC_PARENT_PID at spawn. A daemon thread
polls `os.getppid()`: when the parent dies, the child is re-parented (to
launchd/init), the ppid changes, and the backend exits immediately. This
covers every parent-death mode — including SIGKILL, which no signal handler
in the app could.

`os._exit` (not sys.exit) on purpose: the process must go NOW, not unwind
through atexit hooks and event-loop teardown that may block on in-flight
turns — there is no user left to serve.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

_log = logging.getLogger(__name__)

_INTERVAL_S = 2.0
_started = False


def start_parent_watchdog(
    *,
    getppid: Callable[[], int] = os.getppid,
    interval_s: float = _INTERVAL_S,
    on_orphaned: Optional[Callable[[], None]] = None,
) -> bool:
    """Start the watchdog when ADK_CC_PARENT_PID is set. Returns True when a
    watchdog thread was started. Idempotent per process. `getppid`,
    `interval_s`, and `on_orphaned` are injectable for tests; the production
    orphan action is `os._exit(0)`."""
    global _started
    raw = os.environ.get("ADK_CC_PARENT_PID")
    if not raw or _started:
        return False
    try:
        parent = int(raw)
    except ValueError:
        _log.warning("parent_watchdog: ADK_CC_PARENT_PID=%r is not a pid", raw)
        return False

    action = on_orphaned or _default_orphan_action

    def _watch() -> None:
        while True:
            time.sleep(interval_s)
            current = getppid()
            if current != parent:
                _log.warning(
                    "parent_watchdog: parent %d is gone (ppid now %d) — "
                    "exiting so no orphan holds the port", parent, current)
                action()
                return  # only reachable with an injected test action

    threading.Thread(target=_watch, name="adk-cc-parent-watchdog",
                     daemon=True).start()
    _started = True
    _log.info("parent_watchdog: armed for parent pid %d", parent)
    return True


def _default_orphan_action() -> None:
    """Take the whole process GROUP down, not just this process.

    The backend spawns its own children (run_bash commands, provisioning
    subprocesses); a bare os._exit would re-orphan them one level down. The
    app spawns us as a group leader (process_group(0)), so when we ARE the
    leader, TERM the group (uvicorn gets a graceful-shutdown window, which
    also closes the torn-JSONL risk of exiting mid-append), then KILL it.
    When we are NOT the leader (legacy launch, dev shell), killpg would hit
    unrelated shell members — fall back to a plain exit."""
    import signal

    try:
        if os.getpgrp() == os.getpid():
            os.killpg(os.getpgrp(), signal.SIGTERM)
            time.sleep(3.0)
            os.killpg(os.getpgrp(), signal.SIGKILL)
            return  # unreachable — SIGKILL took us with the group
    except Exception:  # noqa: BLE001 — dying must not fail
        pass
    os._exit(0)


def _reset_for_test() -> None:
    global _started
    _started = False
