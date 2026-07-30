"""Skills written by other people, loaded the way they actually ship.

Every skill exercised until now was written for adk-cc, so the loader was only
ever tested against skills that already agreed with it. Run against Anthropic's
published example-skills, four things broke — all of them silent:

  * `claude-api` never appeared. Its description exceeds ADK's 1024-character
    cap, so validation refused the whole skill, logged on the ROOT logger, and
    the user saw an empty space where a skill they installed should be.
  * `web-artifacts-builder/scripts/init-artifact.sh` failed on its own
    tarball: ADK's loader reads every resource with `read_text("utf-8")` and
    skips whatever raises, so `shadcn-components.tar.gz` was dropped and the
    script printed "not found in script directory".
  * `pdf/scripts/extract_form_field_info.py` died on `No module named 'pypdf'`
    at the end of a traceback — true, and easy to read as "the script is
    broken" rather than "one package is missing here".
  * (measured, not fixed here) docx/pptx/xlsx each materialise ~1.1 MB into
    the launcher on every invocation.

Fixtures below reproduce the first three so they run everywhere; the real
corpus is then used as-is when it happens to be installed, because a fixture
only proves what its author already understood.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_thirdparty_skills.py
"""

from __future__ import annotations

import os
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


def _fixtures() -> Path:
    root = Path(tempfile.mkdtemp(prefix="3p-skills-"))

    # 1. A description past ADK's cap — verbatim in shape to the published one.
    over = "Reference for the API. " * 60          # ~1380 chars
    d = root / "wordy-skill"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: wordy-skill\ndescription: {over}\nlicense: MIT\n---\n\nBody.\n")
    (d / "scripts" / "go.py").write_text("print('wordy ran')\n")

    # 2. A binary sibling next to a script that needs it.
    d = root / "binary-skill"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: binary-skill\ndescription: >\n  Ships a binary blob.\n---\n\nBody.\n")
    (d / "scripts" / "use.sh").write_text(
        'test -f "$(dirname "$0")/blob.bin" && echo FOUND || echo MISSING\n')
    (d / "scripts" / "blob.bin").write_bytes(bytes(range(256)) * 4)
    (d / "assets").mkdir()
    (d / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(200)))

    # 3. Broken beyond repair: this one SHOULD stay rejected.
    d = root / "hopeless-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: totally-different-name\ndescription: x\n---\n")
    return root


def _run_use_sh() -> str:
    """Drive `binary-skill/scripts/use.sh` through the real tool.

    Keeping the resource on the Skill object is only half the promise — the
    launcher still has to write it back out beside the script. That is where
    the first attempt failed: `Script.src` is annotated `str`, pydantic
    rejected the bytes, and the file was quietly dropped again.
    """
    import asyncio

    os.environ.setdefault("ADK_CC_SANDBOX_BACKEND", "noop")
    os.environ.setdefault("ADK_CC_NOOP_ACK_HOST_EXEC", "1")
    ws = tempfile.mkdtemp(prefix="3p-ws-")

    import adk_cc.sandbox as sandbox
    import adk_cc.sandbox.code_executor as ce
    from adk_cc.sandbox.backends.noop_backend import NoopBackend
    from adk_cc.sandbox.workspace import WorkspaceRoot
    from adk_cc.tools import skills as sk

    class _Ws:
        abs_path = ws

        def fs_write_config(self):
            return None

        def fs_read_config(self):
            return None

    sandbox.get_workspace = lambda ctx: _Ws()      # noqa: ARG005
    ce.get_workspace = lambda ctx: _Ws()           # noqa: ARG005

    class _Session:
        def __init__(self):
            self.state = {
                "temp:sandbox_backend": NoopBackend(),
                "temp:sandbox_workspace": WorkspaceRoot(
                    tenant_id="local", session_id="s1", abs_path=ws)}
            self.id, self.user_id, self.app_name = "s1", "u1", "adk_cc"

    class _Ctx:
        agent_name = "coordinator"

        def __init__(self):
            self.state = {}
            self._invocation_context = type("I", (), {"session": _Session()})()

    tool = next(t for t in sk.make_skill_toolset()._tools
                if t.name == "run_skill_script")
    res = asyncio.run(tool.run_async(
        args={"skill_name": "binary-skill", "file_path": "scripts/use.sh"},
        tool_context=_Ctx()))
    return ((res or {}).get("stdout") or "").strip()


def main() -> int:
    root = _fixtures()
    os.environ["ADK_CC_SKILLS_DIR"] = str(root)
    from adk_cc.tools import skill_enablement, skills as sk

    sk.clear_project_skill_cache()
    loaded = {s.frontmatter.name: s for s, _ in sk.discover_skills_with_sources()}

    # --- an over-long description costs the text, not the skill ---------
    check("a skill with an over-long description still loads",
          "wordy-skill" in loaded, f"loaded: {sorted(loaded)}")
    desc = loaded["wordy-skill"].frontmatter.description if "wordy-skill" in loaded else ""
    check("its description is cut to the limit that rejected it",
          0 < len(desc) <= 1024, f"len={len(desc)}")
    check("and the cut is visible rather than silent",
          "truncated" in desc, desc[-60:])

    # --- binaries survive the loader ------------------------------------
    b = loaded.get("binary-skill")
    scripts = sorted(b.resources.list_scripts()) if b else []
    check("a binary sibling is kept, not dropped",
          "blob.bin" in scripts, f"scripts={scripts}")
    check("it is kept as BYTES, so it materialises intact",
          bool(b) and isinstance(b.resources.get_script("blob.bin").src, bytes),
          f"type={type(getattr(b.resources.get_script('blob.bin'), 'src', None))}")
    check("a binary ASSET is kept too",
          bool(b) and isinstance(b.resources.get_asset("logo.png"), bytes))
    check("text next to it stays text",
          bool(b) and isinstance(b.resources.get_script("use.sh").src, str))

    # --- what cannot be repaired is REPORTED -----------------------------
    broken = {u["name"]: u for u in sk.unloadable_skills()}
    check("a genuinely broken skill is still refused",
          "hopeless-skill" not in loaded)
    check("but it is recorded instead of vanishing",
          "hopeless-skill" in broken, f"unloadable={sorted(broken)}")
    check("with a reason a person can act on",
          "name" in (broken.get("hopeless-skill", {}).get("reason") or "").lower(),
          broken.get("hopeless-skill", {}).get("reason", ""))

    rows = {r["name"]: r for r in skill_enablement.catalog()}
    check("the UI catalogue shows it as a problem row",
          rows.get("hopeless-skill", {}).get("problem"),
          f"row={rows.get('hopeless-skill')}")
    check("and it cannot be enabled",
          rows.get("hopeless-skill", {}).get("enabled") is False)

    # --- a missing package is named -------------------------------------
    hinted = sk._explain_missing_package(
        {"stdout": "", "stderr": "Traceback…\nModuleNotFoundError: No module named 'pypdf'",
         "status": "error"}, loaded["binary-skill"])
    check("a missing package is named with what to do",
          "pypdf" in hinted["stderr"] and "NOT RUN" in hinted["stderr"],
          hinted["stderr"][-120:])
    quiet = sk._explain_missing_package(
        {"stdout": "ok", "stderr": "", "status": "success"}, loaded["binary-skill"])
    check("a clean run is left alone", quiet["stderr"] == "")

    # --- and the bytes reach the script, which is the whole point -------
    check("the binary materialises next to the script that needs it",
          _run_use_sh() == "FOUND", f"got {_run_use_sh()!r}")

    # --- the real corpus, when this machine has it ----------------------
    corpus = Path(os.path.expanduser(
        "~/.claude/plugins/cache/anthropic-agent-skills/example-skills"))
    real = sorted(corpus.glob("*/skills")) if corpus.is_dir() else []
    if not real:
        print("\n  (skipped the real-corpus checks: no published skills installed)")
    else:
        base = real[-1]
        os.environ["ADK_CC_SKILLS_DIR"] = str(base)
        sk.clear_project_skill_cache()
        got = {s.frontmatter.name for s, _ in sk.discover_skills_with_sources()}
        on_disk = {d.name for d in base.iterdir()
                   if d.is_dir() and (d / "SKILL.md").is_file()}
        missing = sorted(on_disk - got - {u["name"] for u in sk.unloadable_skills()})
        check(f"every published skill in {base.parent.name} is loaded or explained",
              not missing, f"silently absent: {missing}")
        tar = base / "web-artifacts-builder" / "scripts" / "shadcn-components.tar.gz"
        if tar.is_file():
            sk.clear_project_skill_cache()
            w = {s.frontmatter.name: s
                 for s, _ in sk.discover_skills_with_sources()}["web-artifacts-builder"]
            check("the published skill's tarball is attached",
                  "shadcn-components.tar.gz" in w.resources.list_scripts(),
                  f"{sorted(w.resources.list_scripts())}")

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
