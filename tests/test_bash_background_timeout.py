"""A finished script must not look like a hang (user-reported `exit ?`).

Reported shape: a bash call that starts a server in the background, probes it,
kills it and echoes "Test complete" — shown in the UI as `exit ?` with no
output. Measured cause, in three parts:

  * a backgrounded child inherits the stdout PIPE, so the reader waited for the
    CHILD, not the script — a 6s-timeout command blocked for the full 20s of its
    `sleep 20 &`;
  * `wait_for(communicate())` cancels the read, and the post-kill retry could
    not recover what had already been printed — every line was lost;
  * the timeout payload carried neither an exit code nor a timeout flag, so the
    UI had nothing to render but `exit ?`.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_bash_background_timeout.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
os.environ["ADK_CC_SANDBOX_BACKEND"] = "noop"

_WS = tempfile.mkdtemp(prefix="bashbg-")

import adk_cc.tools.bash.tool as bashmod  # noqa: E402
from adk_cc.tools.schemas import RunBashArgs  # noqa: E402


class _Ctx:
    agent_name = "coordinator"

    def __init__(self):
        self.state = {}


class _Ws:
    abs_path = _WS

    def fs_write_config(self):
        return None

    def fs_read_config(self):
        return None


bashmod.get_workspace = lambda ctx: _Ws()


def _run(cmd: str, timeout: int = 4):
    t0 = time.time()
    res = asyncio.run(bashmod.BashTool()._execute(
        RunBashArgs(command=cmd, timeout_seconds=timeout), _Ctx()))
    return time.time() - t0, res


def test_a_background_child_does_not_outlive_the_timeout() -> None:
    """The call must return at its deadline, not when the orphan finishes."""
    elapsed, res = _run("sleep 30 & echo started; sleep 0.3; echo done", timeout=3)
    assert elapsed < 3 + 3.5, f"took {elapsed:.1f}s for a 3s timeout"
    assert res.get("timed_out") is True, res
    print(f"OK a_background_child_does_not_outlive_the_timeout ({elapsed:.1f}s)")


def test_output_printed_before_the_deadline_survives() -> None:
    """Losing the output is worse than the delay: the user cannot even see how
    far the script got."""
    _, res = _run("sleep 30 & echo started; sleep 0.3; echo done", timeout=3)
    out = res.get("stdout") or ""
    assert "started" in out and "done" in out, res
    print("OK output_printed_before_the_deadline_survives")


def test_the_timeout_is_reported_as_a_timeout() -> None:
    """`exit ?` said nothing. The payload now carries the flag, the deadline,
    and the usual cause."""
    _, res = _run("sleep 30 & echo x", timeout=2)
    assert res["status"] == "timeout" and res["timed_out"] is True, res
    assert res["timeout_seconds"] == 2, res
    assert "background" in (res.get("stderr") or "").lower(), res.get("stderr")
    print("OK the_timeout_is_reported_as_a_timeout")


def test_the_reported_shape_still_completes_normally() -> None:
    """Start in background, probe, kill, wait, echo — the user's script. It
    finishes well inside the timeout and reports exit 0."""
    elapsed, res = _run(
        "sleep 30 & PID=$!; sleep 0.3; echo probing; kill $PID 2>/dev/null; "
        "wait $PID 2>/dev/null; echo 'Test complete'", timeout=10)
    assert res.get("exit_code") == 0, res
    assert "Test complete" in (res.get("stdout") or ""), res
    assert elapsed < 5, f"{elapsed:.1f}s"
    print(f"OK the_reported_shape_still_completes_normally ({elapsed:.1f}s)")


def test_ordinary_commands_are_unaffected() -> None:
    _, res = _run("echo hello && echo world")
    assert res.get("exit_code") == 0 and "world" in res["stdout"], res
    _, res = _run("exit 3")
    assert res.get("exit_code") == 3, res
    print("OK ordinary_commands_are_unaffected")


def main() -> None:
    test_a_background_child_does_not_outlive_the_timeout()
    test_output_printed_before_the_deadline_survives()
    test_the_timeout_is_reported_as_a_timeout()
    test_the_reported_shape_still_completes_normally()
    test_ordinary_commands_are_unaffected()
    print("\nall bash background/timeout tests passed")


if __name__ == "__main__":
    main()
