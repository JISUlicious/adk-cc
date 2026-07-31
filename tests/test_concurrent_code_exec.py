"""Two executions at once must not overwrite each other's file.

Measured live, and only live: asked to find the driver in a CSV, a model ran
`premodel_audit.py` and `collinearity_probe.py` in the same moment. BOTH came
back with

    SyntaxError: unmatched ')'   at .adk-cc/code/scratch.py line 49

— two halves of two different programs in one file. Every execution without an
explicit `execution_id` wrote to that single constant path, so the second write
landed while the first was still being read. Nothing in the unit suite could see
it: each test ran one script at a time.

The name is now derived from the code, so identical code still shares one file
(a retry stays idempotent) and different code never collides.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_concurrent_code_exec.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
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


def main() -> int:
    from google.adk.code_executors.code_execution_utils import CodeExecutionInput

    from adk_cc.sandbox.backends.noop_backend import NoopBackend
    from adk_cc.sandbox.code_executor import SandboxBackedCodeExecutor
    from adk_cc.sandbox.workspace import WorkspaceRoot

    ws_dir = tempfile.mkdtemp(prefix="concur-ws-")

    class _Ctx:
        def __init__(self):
            session = type("S", (), {})()
            session.state = {
                "temp:sandbox_backend": NoopBackend(),
                "temp:sandbox_workspace": WorkspaceRoot(
                    tenant_id="local", session_id="s1", abs_path=ws_dir)}
            session.id, session.user_id, session.app_name = "s1", "u1", "adk_cc"
            self.session = session

    ex = SandboxBackedCodeExecutor()
    ctx = _Ctx()

    # Two DIFFERENT programs, each long enough that a torn write shows up as a
    # syntax error rather than as plausible output.
    programs = {
        "alpha": "x = [\n" + "".join(f"    {i},\n" for i in range(400)) + "]\nprint('alpha', sum(x))\n",
        "beta": "y = {\n" + "".join(f"    {i}: '{i}',\n" for i in range(400)) + "}\nprint('beta', len(y))\n",
    }
    results: dict[str, str] = {}

    def run(tag: str) -> None:
        for _ in range(4):        # repeat: a race that fires once in four is a race
            r = ex.execute_code(ctx, CodeExecutionInput(code=programs[tag]))
            results[tag] = ((r.stdout or "") + (r.stderr or ""))
            if tag not in results or "Error" in results[tag]:
                break

    threads = [threading.Thread(target=run, args=(t,)) for t in programs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for tag, expect in (("alpha", "alpha 79800"), ("beta", "beta 400")):
        out = results.get(tag, "")
        check(f"{tag} ran its OWN program",
              expect in out, " ".join(out.split())[:160] or "(no output)")
        check(f"{tag} did not pick up the other program's text",
              "SyntaxError" not in out and "unmatched" not in out,
              " ".join(out.split())[-160:])

    # Concurrency aside, the file must not be one shared constant any more.
    code_dir = Path(ws_dir) / ".adk-cc" / "code"
    names = sorted(p.name for p in code_dir.iterdir()) if code_dir.is_dir() else []
    check("each distinct program got its own file",
          len([n for n in names if n.endswith(".py")]) >= 2, names)
    check("and an identical re-run reuses one file rather than piling up",
          len(names) <= 4, names)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
