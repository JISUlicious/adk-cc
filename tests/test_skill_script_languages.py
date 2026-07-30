"""A skill script does not have to be Python.

ADK's launcher handles `.py`, `.sh` and `.bash`; anything else returns
"Unsupported script type". That is the launcher's limit, not a sensible one for
skill authors — a skill is a folder of files, and the first one here to ship a
Node runner could not be started at all. The workaround was a `.py` shim beside
it, which taxes every author for a launcher limitation.

So `_build_wrapper_code` is extended to emit ADK's own `__shell_result__`
envelope for other interpreters, reusing the surrounding machinery
(materialisation of the WHOLE skill, argv conventions, timeout, result parsing)
rather than forking the tool.

Driven through the real tool against real temp skills, because the interesting
parts are exactly the ones a unit test of the string would miss: whether the
interpreter is found, whether siblings materialise, whether args arrive.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_skill_script_languages.py
"""

from __future__ import annotations

import asyncio
import json
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

_ROOT = Path(tempfile.mkdtemp(prefix="langskills-"))
_WS = Path(tempfile.mkdtemp(prefix="langws-"))
os.environ["ADK_CC_SKILLS_DIR"] = str(_ROOT)
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _skill(name: str, scripts: dict[str, str]) -> None:
    d = _ROOT / name
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: >\n  Test skill {name}.\n---\n\nBody.\n")
    for rel, body in scripts.items():
        (d / "scripts" / rel).write_text(body)


# A Node entrypoint that imports a SIBLING and echoes its args — the exact
# shape that failed before (web-smoke-check's runner + its helpers).
_skill("node-skill", {
    "main.mjs": (
        "import { shout } from './helper.mjs';\n"
        "console.log(JSON.stringify({said: shout(process.argv[2] || 'nothing'),\n"
        "  argv: process.argv.slice(2)}));\n"
    ),
    "helper.mjs": "export const shout = (s) => String(s).toUpperCase();\n",
})
_skill("cjs-skill", {"main.js": "console.log('cjs ok:' + process.argv[2]);\n"})
_skill("bad-skill", {"boom.mjs": "console.error('kaboom');\nprocess.exit(3);\n"})
_skill("ps-skill", {"main.ps1": "Write-Output 'ps ok'\n"})
_skill("exe-skill", {"main.exe": "not really a binary\n"})


class _Ws:
    abs_path = str(_WS)

    def fs_write_config(self):
        return None

    def fs_read_config(self):
        return None


class _Session:
    def __init__(self):
        from adk_cc.sandbox.backends.noop_backend import NoopBackend
        from adk_cc.sandbox.workspace import WorkspaceRoot

        self.state = {
            "temp:sandbox_backend": NoopBackend(),
            "temp:sandbox_workspace": WorkspaceRoot(
                tenant_id="local", session_id="s1", abs_path=str(_WS)),
        }
        self.id, self.user_id, self.app_name = "s1", "u1", "adk_cc"


class _Inv:
    def __init__(self):
        self.session = _Session()


class _Ctx:
    agent_name = "coordinator"

    def __init__(self):
        self.state = {}
        self._invocation_context = _Inv()


def _run(skill: str, file_path: str, args=None):
    import adk_cc.sandbox as sandbox
    import adk_cc.sandbox.code_executor as ce
    from adk_cc.tools import skills as sk

    sandbox.get_workspace = lambda ctx: _Ws()      # noqa: ARG005
    ce.get_workspace = lambda ctx: _Ws()           # noqa: ARG005
    sk.clear_project_skill_cache()
    toolset = sk.make_skill_toolset()
    tool = next(t for t in toolset._tools if t.name == "run_skill_script")
    return asyncio.run(tool.run_async(
        args={"skill_name": skill, "file_path": file_path,
              **({"args": args} if args is not None else {})},
        tool_context=_Ctx()))


def main() -> int:
    if not shutil.which("node"):
        print("SKIP: node not available."); return 0

    res = _run("node-skill", "scripts/main.mjs", ["hello"])
    out = (res or {}).get("stdout") or ""
    err = (res or {}).get("stderr") or ""
    check("a .mjs entrypoint runs at all", bool(out.strip()),
          f"stdout empty; stderr={err[:200]!r}")
    payload = {}
    for line in out.splitlines():
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    check("its SIBLING module resolves", payload.get("said") == "HELLO",
          f"got {payload or out[:160]!r} — the whole skill must materialise")
    check("arguments arrive", payload.get("argv") == ["hello"],
          f"argv={payload.get('argv')!r}")

    res = _run("cjs-skill", "scripts/main.js", ["x"])
    check("a plain .js entrypoint runs",
          "cjs ok:x" in ((res or {}).get("stdout") or ""),
          f"{res}")

    # A script that fails must READ as failed. ADK unwraps the result envelope
    # only for .sh/.bash, so before this was handled a non-zero .mjs came back
    # as `status: success` with the raw JSON envelope as its stdout — the worst
    # kind of wrong, since the agent would report the step as having passed.
    res = _run("bad-skill", "scripts/boom.mjs") or {}
    check("a failing script reports status=error", res.get("status") == "error",
          f"status={res.get('status')!r} stdout={str(res.get('stdout'))[:120]!r}")
    check("its stderr is surfaced, not left inside the envelope",
          "kaboom" in (res.get("stderr") or ""), f"{res.get('stderr')!r}")
    check("the envelope itself never reaches the caller",
          "__shell_result__" not in json.dumps(res), str(res)[:160])

    # A .ps1 on a machine without pwsh: the point is the MESSAGE, since this is
    # how a Windows-authored skill lands on a Linux box.
    res = _run("ps-skill", "scripts/main.ps1")
    text = ((res or {}).get("stdout") or "") + ((res or {}).get("stderr") or "")
    if shutil.which("pwsh"):
        check("a .ps1 runs where pwsh exists", "ps ok" in text, text[:160])
    else:
        check("a missing interpreter is named, not just a non-zero exit",
              "pwsh" in text and "not installed" in text, text[:200])
        check("and it says not to silently substitute",
              "substitute" in text, text[:200])

    # Something genuinely unsupported must say what IS supported, rather than
    # ADK's ".py, .sh, .bash" — which is no longer true of this tool.
    res = _run("exe-skill", "scripts/main.exe")
    err = json.dumps(res or {})
    check("an unsupported type lists the real supported set",
          "UNSUPPORTED_SCRIPT_TYPE" in err and ".mjs" in err and ".py" in err,
          err[:220])

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
