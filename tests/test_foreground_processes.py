"""#131: foreground executions in the process registry.

Every code/skill run through SandboxBackedCodeExecutor becomes a visible,
tail-able, (noop:) stoppable process record. The load-bearing checks:
result parity (stdout/stderr separation byte-identical to the old buffered
path), LIVE tail while the script runs, Stop mid-run preserving partial
output, timeout partial output, and the foreground history cap.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_foreground_processes.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
os.environ.setdefault("ADK_CC_NOOP_ACK_HOST_EXEC", "1")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {str(detail)[:120]}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


_DATA = tempfile.mkdtemp(prefix="fgproc-data-")
os.environ["ADK_CC_DATA_DIR"] = _DATA
# get_registry() prefers desktop_data_dir() — pin it too, or the suite
# attaches to the REAL ~/.adk-cc-desktop registry (found the hard way).
os.environ["ADK_CC_DESKTOP_DATA"] = _DATA
_WS = tempfile.mkdtemp(prefix="fgproc-ws-")


class _Ws:
    abs_path = _WS

    def fs_write_config(self):
        return None

    def fs_read_config(self):
        return None


def _executor_and_ctx():
    from adk_cc.sandbox.backends.noop_backend import NoopBackend
    from adk_cc.sandbox.code_executor import SandboxBackedCodeExecutor
    from adk_cc.sandbox.workspace import WorkspaceRoot

    class _Session:
        def __init__(self):
            self.state = {
                "temp:sandbox_backend": NoopBackend(),
                "temp:sandbox_workspace": WorkspaceRoot(
                    tenant_id="local", session_id="s1", abs_path=_WS),
            }
            self.id, self.user_id, self.app_name = "s1", "u1", "adk_cc"

    class _Inv:
        def __init__(self):
            self.session = _Session()

    ex = SandboxBackedCodeExecutor()
    return ex, _Inv()


def _run(ex, inv, code, execution_id=None):
    from google.adk.code_executors.code_execution_utils import CodeExecutionInput

    return ex.execute_code(inv, CodeExecutionInput(
        code=code, execution_id=execution_id))


def _registry():
    from adk_cc.sandbox.process_registry import get_registry

    return get_registry()


def main() -> int:
    ex, inv = _executor_and_ctx()

    # ---- parity: stdout/stderr separation preserved ----------------------
    r = _run(ex, inv, "import sys\nprint('to out')\n"
                      "print('to err', file=sys.stderr)\n")
    check("parity: stdout intact", r.stdout.strip() == "to out", r.stdout)
    check("parity: stderr separate", r.stderr.strip() == "to err", r.stderr)
    recs = [x for x in _registry().list() if x.kind == "foreground"]
    check("record created and finalized",
          len(recs) == 1 and recs[0].status == "exited"
          and recs[0].exit_code == 0, [(x.status, x.exit_code) for x in recs])
    check("record label is derived from the code",
          recs[0].label.startswith("code run "), recs[0].label)
    log = _registry().read_log(recs[0].id)
    check("panel log holds stdout + folded stderr",
          "to out" in log and "--- stderr ---" in log and "to err" in log,
          log[:120])

    # ---- skill marker → label -------------------------------------------
    r = _run(ex, inv, "# adk-cc-skill-tiers: base\n"
                      "# adk-cc-skill-script: data-analyst scripts/probe.py\n"
                      "print('ok')\n")
    lab = [x for x in _registry().list() if x.kind == "foreground"][0].label
    check("skill marker labels the record",
          lab == "skill: data-analyst scripts/probe.py", lab)

    # ---- LIVE tail while running ----------------------------------------
    slow = ("import sys, time\n"
            "print('started work', flush=True)\n"
            "time.sleep(8)\n"
            "print('finished')\n")
    box = {}

    def _bg():
        ex2, inv2 = _executor_and_ctx()
        box["res"] = _run(ex2, inv2, slow, execution_id="slow-live")

    t = threading.Thread(target=_bg)
    t.start()
    live_seen = running_seen = False
    live_rec = None
    for _ in range(60):
        time.sleep(0.25)
        fg = [x for x in _registry().list() if x.kind == "foreground"
              and x.status == "running"]
        if fg:
            running_seen = True
            live_rec = fg[0]
            if "started work" in _registry().read_log(fg[0].id):
                live_seen = True
                break
    check("record visible as RUNNING while the script runs", running_seen)
    check("log tail is LIVE (early print visible mid-run)", live_seen)
    check("running record carries the timeout budget",
          live_rec is not None and live_rec.timeout_s == ex.timeout_seconds)
    check("running record offers Stop (noop)",
          live_rec is not None and live_rec.can_terminate)
    t.join(timeout=30)
    check("slow run finished with full output",
          "finished" in box["res"].stdout, box["res"].stdout[:80])

    # ---- Stop mid-run: partial output preserved, record 'killed' ---------
    def _bg2():
        ex3, inv3 = _executor_and_ctx()
        box["res2"] = _run(ex3, inv3,
                           "import time\nprint('early print', flush=True)\n"
                           "time.sleep(60)\nprint('unreachable')\n",
                           execution_id="slow-stop")

    t2 = threading.Thread(target=_bg2)
    t2.start()
    stopped = False
    for _ in range(80):
        time.sleep(0.25)
        fg = [x for x in _registry().list() if x.kind == "foreground"
              and x.status == "running" and "slow-stop" in (x.command or "")
              or x.kind == "foreground" and x.status == "running"]
        fg = [x for x in fg if "early print" in _registry().read_log(x.id)]
        if fg:
            stopped = _registry().terminate(fg[0].id)
            break
    t2.join(timeout=30)
    check("Stop mid-run succeeds", stopped)
    check("turn survives: result carries the PARTIAL output",
          "early print" in box.get("res2").stdout
          and "unreachable" not in box["res2"].stdout,
          box["res2"].stdout[:80])
    killed = [x for x in _registry().list()
              if x.kind == "foreground" and x.status == "killed"]
    check("record verdict stays 'killed' (finalize never downgrades)",
          len(killed) == 1, [(x.status, x.label) for x in killed])

    # ---- timeout: partial output preserved -------------------------------
    ex4, inv4 = _executor_and_ctx()
    ex4.timeout_seconds = 3
    r4 = _run(ex4, inv4, "import time\nprint('before the wall', flush=True)\n"
                         "time.sleep(30)\n", execution_id="slow-timeout")
    check("timeout: partial stdout preserved via the redirected file",
          "before the wall" in r4.stdout, r4.stdout[:80])
    check("timeout: the clock note still lands", "did not finish" in r4.stderr,
          r4.stderr[:80])
    to = [x for x in _registry().list() if x.kind == "foreground"][0]
    check("timeout record marked failed", to.status == "failed", to.status)

    # ---- history cap ------------------------------------------------------
    ex5, inv5 = _executor_and_ctx()
    for i in range(25):
        _run(ex5, inv5, f"print({i})\n", execution_id=f"cap-{i}")
    fg_done = [x for x in _registry().list() if x.kind == "foreground"
               and x.status not in ("running", "starting")]
    check("foreground history capped at 20", len(fg_done) <= 20, len(fg_done))
    bg_like = [x for x in _registry().list() if x.kind == "background"]
    check("background records untouched by the cap", len(bg_like) == 0)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
