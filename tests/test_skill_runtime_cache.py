"""A skill's scripts live in the workspace, and are written there once.

ADK materialises the WHOLE skill into a `TemporaryDirectory` per invocation and
runs the target inside it. Measured consequences, both on published skills:

  * anything the script CREATES is deleted when the call returns.
    `web-artifacts-builder` scaffolds a project — under a temp cwd its work is
    thrown away, and it failed outright because it uses its argument as both a
    project name and a `cd` target.
  * the payload is re-sent every time. docx, pptx and xlsx carry ~1.1 MB of
    XSD schemas each, which crosses the wire on every call to a remote backend.

So the skill is materialised once into `.adk-cc/skill-runtime/<name>/<digest>/`
and the script runs as a subprocess with cwd = the workspace. This test drives
the real tool and looks at the real filesystem, because both claims are about
what survives the call.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_skill_runtime_cache.py
"""

from __future__ import annotations

import asyncio
import os
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

_ROOT = Path(tempfile.mkdtemp(prefix="cacheskills-"))
_WS = Path(tempfile.mkdtemp(prefix="cachews-"))
os.environ["ADK_CC_SKILLS_DIR"] = str(_ROOT)

_passed = _failed = 0
SENT: list[str] = []   # the code handed to the sandbox, per call


BULK = "lorem ipsum"          # only ever present in the skill's own bytes


def _big(codes) -> int:
    return max((len(c) for c in codes), default=0)


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _write_skill(padding: str = "x") -> None:
    d = _ROOT / "maker"
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\nname: maker\ndescription: >\n  Creates a file.\n---\n\nBody.\n")
    (d / "scripts" / "make.py").write_text(
        "import sys\n"
        "from helper import stamp\n"          # sibling import, no sys.path help
        "open(sys.argv[1], 'w').write(stamp())\n"
        "print('wrote ' + sys.argv[1])\n")
    (d / "scripts" / "helper.py").write_text(
        f"def stamp():\n    return 'made-by-skill-{padding}'\n")
    # Bulk, so a re-sent payload is unmistakable in the measurements.
    (d / "references").mkdir(exist_ok=True)
    (d / "references" / "big.md").write_text(("lorem ipsum " * 40 + "\n") * 300)


class _Ws:
    abs_path = str(_WS)

    def fs_write_config(self):
        return None

    def fs_read_config(self):
        return None


class _Ctx:
    agent_name = "coordinator"

    def __init__(self):
        from adk_cc.sandbox.backends.noop_backend import NoopBackend
        from adk_cc.sandbox.workspace import WorkspaceRoot

        self.state = {}
        session = type("S", (), {})()
        session.state = {
            "temp:sandbox_backend": NoopBackend(),
            "temp:sandbox_workspace": WorkspaceRoot(
                tenant_id="local", session_id="s1", abs_path=str(_WS))}
        session.id, session.user_id, session.app_name = "s1", "u1", "adk_cc"
        self._invocation_context = type("I", (), {"session": session})()


def _run(args):
    import adk_cc.sandbox as sandbox
    import adk_cc.sandbox.code_executor as ce
    from adk_cc.tools import skills as sk

    sandbox.get_workspace = lambda ctx: _Ws()      # noqa: ARG005
    ce.get_workspace = lambda ctx: _Ws()           # noqa: ARG005
    tool = next(t for t in sk.make_skill_toolset()._tools
                if t.name == "run_skill_script")
    return asyncio.run(tool.run_async(
        args={"skill_name": "maker", "file_path": "scripts/make.py", "args": args},
        tool_context=_Ctx()))


def test_runtime_dir_failures_are_survivable() -> None:
    """Reported live alongside #109: running a skill script died with "no such
    directory" for the skill-runtime dir. Three ways the wrapper could produce
    that, all now handled:

      1. a flat file set — `makedirs(dirname(f))` only ever created the digest
         dir as a SIDE EFFECT of a file in a subdirectory;
      2. a stale `.ready` with the tree gone (a checkpoint restore reverting
         `.adk-cc/**`, a manual clean, an interrupted write) — the probe said
         "warm" and the run then failed on the missing script;
      3. an empty payload — died on `open(.ready)` with a bare ENOENT that
         pointed at the runtime dir instead of the real problem.
    """
    import subprocess
    import sys as _sys
    import tempfile

    from adk_cc.tools.skills import _WiderScriptCodeExecutor

    ex = _WiderScriptCodeExecutor(base_executor=None, script_timeout=60)
    cache = ".adk-cc/skill-runtime/demo/abc123"
    files = {"scripts/run.py": "print('ran ok')\n", "SKILL.md": "x\n"}

    def run(tmp: str, wrapper: str) -> str:
        p = os.path.join(tmp, "w.py")
        with open(p, "w") as fh:
            fh.write(wrapper)
        r = subprocess.run([_sys.executable, p], cwd=tmp,
                           capture_output=True, text=True)
        return (r.stdout or "") + (r.stderr or "")

    with tempfile.TemporaryDirectory() as tmp:
        cold = run(tmp, ex._wrapper(cache, "scripts/run.py", [], None, [], []))
        assert "__needs_materialize__" in cold, cold[:200]
        warm = run(tmp, ex._wrapper(cache, "scripts/run.py", [], files, [], []))
        assert "ran ok" in warm, warm[:200]
        assert os.path.isdir(os.path.join(tmp, cache))

    # Stale marker, missing script -> re-materialise instead of ENOENT.
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, cache), exist_ok=True)
        with open(os.path.join(tmp, cache, ".ready"), "w") as fh:
            fh.write("1")
        out = run(tmp, ex._wrapper(cache, "scripts/run.py", [], None, [], []))
        assert "__needs_materialize__" in out, out[:200]

    # Empty payload -> an explanation, not a path error.
    with tempfile.TemporaryDirectory() as tmp:
        out = run(tmp, ex._wrapper(cache, "scripts/run.py", [], {}, [], []))
        assert "no files to materialise" in out, out[:200]
        assert "No such file or directory" not in out, out[:200]

    print("OK test_runtime_dir_failures_are_survivable")


def main() -> int:
    test_runtime_dir_failures_are_survivable()
    _write_skill()
    import adk_cc.sandbox.code_executor as ce
    from adk_cc.tools import skills as sk

    # Every byte the launcher hands to the sandbox, per call.
    original = ce.SandboxBackedCodeExecutor.execute_code

    def _spy(self, ctx, cei):
        SENT.append(cei.code or "")
        return original(self, ctx, cei)

    ce.SandboxBackedCodeExecutor.execute_code = _spy
    sk.clear_project_skill_cache()

    # --- the output survives the call -----------------------------------
    res = _run(["out.txt"])
    made = _WS / "out.txt"
    check("a relative path resolves against the WORKSPACE, not a temp dir",
          made.is_file(), f"{res}")
    check("its contents are the script's own",
          made.is_file() and made.read_text().startswith("made-by-skill"),
          made.read_text()[:60] if made.is_file() else "(absent)")
    check("the script still ran successfully", (res or {}).get("status") == "success",
          f"{res}")
    check("a sibling import works without any sys.path help",
          "wrote out.txt" in ((res or {}).get("stdout") or ""), f"{res}")

    runtime = _WS / ".adk-cc" / "skill-runtime" / "maker"
    digests = sorted(p.name for p in runtime.iterdir()) if runtime.is_dir() else []
    check("the skill is materialised under the workspace", len(digests) == 1, digests)
    check("and it is NOT under .adk-cc/skills, where project skills live",
          not (_WS / ".adk-cc" / "skills").exists())

    cold = list(SENT)
    SENT.clear()

    # --- a second call ships nothing ------------------------------------
    res2 = _run(["out2.txt"])
    check("the second run works too", (_WS / "out2.txt").is_file(), f"{res2}")
    # The claim is not "smaller" but "does not carry the skill": BULK is only
    # ever in the reference file, so its absence is the payload's absence.
    check("the cold run did carry the skill's bytes",
          any(BULK in c for c in cold))
    check("the warm run carries none of them",
          SENT and not any(BULK in c for c in SENT),
          f"warm payload {_big(SENT)} bytes")
    # Bounded RELATIVE to the cold call, not by a magic byte count. The point
    # is that a warm call does not carry the skill — an absolute cap re-breaks
    # every time the launcher legitimately grows (it did: the read-only-rootfs
    # dependency fallback added ~500 bytes and tipped a 4096 bound to 4411,
    # while the payload was still 34x smaller than cold). The absolute ceiling
    # stays, generously, purely to catch unbounded bloat.
    check("so a warm call is a constant, tiny exchange",
          SENT and _big(SENT) < _big(cold) / 10 and _big(SENT) < 16384,
          f"{_big(SENT)} bytes vs cold {_big(cold)}")

    # Nothing is remembered in-process: the probe has to find the ready marker
    # on disk, which is what makes this survive a restart.
    SENT.clear()
    _run(["out3.txt"])
    check("a new process reuses the materialised copy",
          SENT and not any(BULK in c for c in SENT),
          f"warm-after-restart={_big(SENT)} bytes")

    # --- editing the skill re-materialises ------------------------------
    _write_skill(padding="EDITED")
    sk.clear_project_skill_cache()
    SENT.clear()
    _run(["out4.txt"])
    check("an edited skill is re-sent",
          any(BULK in c for c in SENT), f"{_big(SENT)} bytes")
    check("the edit takes effect",
          (_WS / "out4.txt").read_text().endswith("EDITED"),
          (_WS / "out4.txt").read_text()[:60])
    digests = sorted(p.name for p in runtime.iterdir()) if runtime.is_dir() else []
    check("and the stale copy is pruned rather than accumulating",
          len(digests) == 1, digests)

    ce.SandboxBackedCodeExecutor.execute_code = original
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
