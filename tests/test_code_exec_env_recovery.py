"""A vanished interpreter must self-heal, not fail the run.

Reported live, right after a sandbox image rebuild:

    .adk-cc/analysis-env/bin/python: No such file or directory

`ensure_env` now detects that on disk (test_analysis_env covers it). This
covers the hole that fix CANNOT reach: the in-process cache. Once a workspace
is verified, `ensure_env` returns from `_verified` without probing at all — so
a sandbox REPLACED mid-session (a recreated container, an expired remote
workspace) still hands back a dead interpreter, with the same message, for as
long as the process lives.

The executor's answer is to retry from the observed failure — exit 127 is the
shell's "command not found" — rather than probing defensively before every run
and paying a round trip on the happy path.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_code_exec_env_recovery.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
os.environ.setdefault("ADK_CC_SANDBOX_BACKEND", "noop")
os.environ.setdefault("ADK_CC_NOOP_ACK_HOST_EXEC", "1")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:  # noqa: PLR0915
    if not shutil.which("uv"):
        print("SKIP: uv not installed."); return 0

    from google.adk.code_executors.code_execution_utils import CodeExecutionInput

    from adk_cc.sandbox import analysis_env
    from adk_cc.sandbox.backends.noop_backend import NoopBackend
    from adk_cc.sandbox.code_executor import SandboxBackedCodeExecutor
    from adk_cc.sandbox.workspace import WorkspaceRoot

    for k in ("ADK_CC_ANALYSIS_ENV", "ADK_CC_ANALYSIS_TIERS"):
        os.environ.pop(k, None)
    analysis_env.reset_cache()

    ws_dir = tempfile.mkdtemp(prefix="envrecov-ws-")

    class _Ctx:
        def __init__(self):
            session = type("S", (), {})()
            session.state = {
                "temp:sandbox_backend": NoopBackend(),
                "temp:sandbox_workspace": WorkspaceRoot(
                    tenant_id="local", session_id="s1", abs_path=ws_dir)}
            session.id, session.user_id, session.app_name = "s1", "u1", "adk_cc"
            self.session = session

    ex, ctx = SandboxBackedCodeExecutor(), _Ctx()
    ex.timeout_seconds = 300          # a cold rebuild downloads an interpreter

    # WorkspaceRoot canonicalises in __post_init__ (/var -> /private/var on
    # macOS), and the cache is keyed by that canonical path — comparing against
    # the raw mkdtemp string silently matches nothing and makes this test look
    # like the cache is empty when it is not.
    ws_key = ctx.session.state["temp:sandbox_workspace"].abs_path

    # The retry is invisible in the result by design (a recovered run looks
    # exactly like a normal one), so watch the log to prove it actually fired
    # rather than inferring it from success.
    import logging

    class _Catch(logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs: list[str] = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    catcher = _Catch()
    exec_log = logging.getLogger("adk_cc.sandbox.code_executor")
    exec_log.addHandler(catcher)
    exec_log.setLevel(logging.WARNING)

    try:
        # 1. A normal run, which also populates the in-process cache.
        code = "import sys; print('RAN', sys.version.split()[0])"
        r1 = ex.execute_code(ctx, CodeExecutionInput(code=code))
        check("a normal execution works", "RAN" in (r1.stdout or ""),
              f"{r1.stdout!r} {r1.stderr!r}")

        env_bin = Path(ws_dir, ".adk-cc", "analysis-env", "bin")
        cached = [k for k in analysis_env._verified if k[0] == ws_key]
        check("the workspace is now cached in-process", bool(cached), str(cached))

        # 2. Destroy the interpreter WITHOUT touching the cache — exactly what
        #    a replaced sandbox looks like to a still-running process.
        for p in env_bin.iterdir():
            if p.name.startswith("python"):
                p.unlink()
                p.symlink_to("/nonexistent/uv/python/gone/bin/python3")
        interp = env_bin / "python"
        check("the interpreter is gone", not interp.exists(), str(interp))
        check("but the process still believes it is verified",
              bool([k for k in analysis_env._verified if k[0] == ws_key]),
              "if this fails the run below never exercises the CACHE path, "
              "only the on-disk one")

        # 3. The run that used to surface the raw shell error.
        catcher.msgs.clear()
        r2 = ex.execute_code(ctx, CodeExecutionInput(code=code))
        out = (r2.stdout or "") + (r2.stderr or "")
        check("the execution RECOVERS instead of failing",
              "RAN" in (r2.stdout or ""), " ".join(out.split())[:200])
        check("recovery came from the retry, not from luck",
              any("interpreter missing" in m for m in catcher.msgs),
              f"log: {catcher.msgs}")
        check("the user never sees 'No such file or directory'",
              "No such file or directory" not in out,
              " ".join(out.split())[:200])
        check("and the interpreter is real again",
              (env_bin / "python").exists())

        # 4. A genuine non-zero exit must still be reported, not retried into
        #    silence — the retry keys on 127, not on "any failure".
        bad = ex.execute_code(ctx, CodeExecutionInput(
            code="import sys; sys.stderr.write('BOOM\\n'); sys.exit(3)"))
        check("a real script failure is still surfaced",
              "BOOM" in ((bad.stdout or "") + (bad.stderr or "")),
              f"{bad.stdout!r} {bad.stderr!r}")
    finally:
        exec_log.removeHandler(catcher)
        shutil.rmtree(ws_dir, ignore_errors=True)
        analysis_env.reset_cache()

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
