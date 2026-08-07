"""The generated skill-script launcher must RUN, not merely look right.

The launcher is Python source assembled from a list of strings, some of which
are f-strings evaluated HERE and some plain strings evaluated THERE. Mixing
the two is easy and silent:

    "  _depnote = (f'{NOTE_PREFIX} installing ' + …",   # plain -> literal text

That line looks correct in review and reads correctly in a diff. It ships
`f'{NOTE_PREFIX} …'` into a process whose namespace holds only what the
preamble imports, so it raised

    NameError: name 'NOTE_PREFIX' is not defined

and the launcher died with a traceback instead of reporting why a dependency
install failed. Introduced by the branding commit, which swapped a hardcoded
'[adk-cc]' for {NOTE_PREFIX} in strings that were never f-strings, and
invisible to every existing test because they all assert on the launcher
SOURCE rather than executing it.

So this executes the real generated launcher, through the branches that build
those notes. Any future name that exists only in this module's scope fails
here immediately.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_skill_wrapper_executes.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _run_wrapper(deps, script_body="print('SCRIPT RAN')\n"):
    """Generate the real launcher, run it, return (proc, workspace)."""
    from adk_cc.tools.skills import _WiderScriptCodeExecutor

    ws = tempfile.mkdtemp(prefix="wrapexec-")
    cache = os.path.join(ws, ".adk-cc/skill-runtime/demo/abc123")
    os.makedirs(os.path.join(cache, "scripts"), exist_ok=True)
    open(os.path.join(cache, ".ready"), "w").write("1")
    open(os.path.join(cache, "scripts/s.py"), "w").write(script_body)

    ex = _WiderScriptCodeExecutor.__new__(_WiderScriptCodeExecutor)
    code = ex._wrapper(cache, "scripts/s.py", [], files=None, tiers=[],
                       deps=deps)
    src = os.path.join(ws, "launcher.py")
    open(src, "w").write(code)
    return subprocess.run([sys.executable, src], capture_output=True,
                          text=True, cwd=ws, timeout=600), ws


def main() -> int:
    # 1. The happy path: no deps, the launcher must run the script.
    proc, _ = _run_wrapper(deps=[])
    check("the launcher runs at all", proc.returncode == 0,
          f"exit={proc.returncode} {proc.stderr[-300:]}")
    check("and it runs the script", "SCRIPT RAN" in proc.stdout,
          f"{proc.stdout[:200]!r}")

    # 2. The branch that broke: a dependency that cannot resolve, so the
    #    launcher takes the install-FAILED path and builds the note.
    proc, _ = _run_wrapper(deps=["adk-cc-no-such-package-xyz"])
    check("a failed dep install does not crash the launcher",
          proc.returncode == 0 and "NameError" not in proc.stderr,
          f"exit={proc.returncode} {proc.stderr[-300:]}")
    check("no undefined name leaked into the generated code",
          "NameError" not in (proc.stdout + proc.stderr),
          (proc.stdout + proc.stderr)[-300:])
    check("the failure is REPORTED, with the brand prefix resolved",
          "installing adk-cc-no-such-package-xyz failed" in proc.stdout
          and "{NOTE_PREFIX}" not in proc.stdout,
          f"{proc.stdout[:300]!r}")
    # The script still runs — a dep note must not stop the work.
    check("the script still ran despite the failed install",
          "SCRIPT RAN" in proc.stdout, f"{proc.stdout[:200]!r}")

    # 3. Belt and braces: every name the launcher references must resolve.
    #    compile() catches syntax, not scope, so run a name-resolution pass
    #    over the source by executing it — already done above — and assert the
    #    preamble actually defines the branded prefix.
    from adk_cc.branding import NOTE_PREFIX
    from adk_cc.tools.skills import _WiderScriptCodeExecutor

    ex = _WiderScriptCodeExecutor.__new__(_WiderScriptCodeExecutor)
    src = ex._wrapper("/tmp/x", "s.py", [], files=None, tiers=[], deps=["z"])
    check("the launcher defines NOTE_PREFIX for its own scope",
          f"NOTE_PREFIX = {NOTE_PREFIX!r}" in src,
          "preamble must bake the value in, not rely on the parent process")

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
