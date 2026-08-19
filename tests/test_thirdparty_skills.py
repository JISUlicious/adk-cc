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

    # 3. Renamed on install — the commonest real-world breakage. The implementer
    #    guide says warn and LOAD; adk-cc used to refuse it.
    d = root / "renamed-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: original-name\ndescription: Still perfectly usable.\n---\n\nBody.\n")

    # 4. The malformed YAML the guide calls out by name: an unquoted colon.
    d = root / "colon-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: colon-skill\ndescription: Use this skill when: the user "
        "asks about PDFs\n---\n\nBody.\n")

    # 5. Fatal per the guide: no description means the model can never know
    #    when to use it, so there is nothing to disclose.
    d = root / "mute-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: mute-skill\n---\n\nBody.\n")

    # 6. Fatal per the guide: frontmatter that cannot be parsed at all.
    d = root / "garbage-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("not frontmatter at all\njust text\n")
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

    # --- lenient where the guide says lenient ----------------------------
    # "Name doesn't match the parent directory name → warn, load anyway."
    diags = sk.skill_diagnostics()
    check("a skill whose folder was renamed still loads",
          "original-name" in loaded, f"loaded: {sorted(loaded)}")
    mismatch = [d["message"] for d in diags.get("original-name", [])
                if d["severity"] == "warning"]
    check("and the mismatch is reported rather than silently accepted",
          any("does not match the directory" in m for m in mismatch), mismatch)

    # The guide's named malformation: `description: Use this skill when: …`
    check("an unquoted colon in a value is recovered, not fatal",
          "colon-skill" in loaded, f"loaded: {sorted(loaded)}")
    check("and the recovered description is the real one",
          "PDFs" in (loaded["colon-skill"].frontmatter.description
                     if "colon-skill" in loaded else ""),
          loaded.get("colon-skill") and loaded["colon-skill"].frontmatter.description)

    # --- fatal where the guide says fatal --------------------------------
    broken = {u["name"]: u for u in sk.unloadable_skills()}
    check("no description is fatal — there is nothing to disclose",
          "mute-skill" not in loaded and "mute-skill" in broken,
          f"unloadable={sorted(broken)}")
    check("unparseable frontmatter is fatal",
          "garbage-skill" not in loaded and "garbage-skill" in broken,
          f"unloadable={sorted(broken)}")
    check("each says which of the two it was",
          "description" in (broken.get("mute-skill", {}).get("reason") or "")
          and "YAML" in (broken.get("garbage-skill", {}).get("reason") or ""),
          f"{broken.get('mute-skill')} | {broken.get('garbage-skill')}")

    rows = {r["name"]: r for r in skill_enablement.catalog()}
    check("the UI catalogue shows a fatal one as a problem row",
          rows.get("mute-skill", {}).get("problem"),
          f"row={rows.get('mute-skill')}")
    check("and it cannot be enabled",
          rows.get("mute-skill", {}).get("enabled") is False)
    check("a tolerated one appears as a normal row carrying notes",
          rows.get("original-name", {}).get("enabled") is True
          and rows.get("original-name", {}).get("notes"),
          f"row={rows.get('original-name')}")
    check("and it is NOT presented as a problem",
          not rows.get("original-name", {}).get("problem"))

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

    # Client report (sessions 4617ffb2/623b7f6a/1141ab6d): a bare
    # `uv pip install X` remedy is the first rung of a ladder that cannot
    # work from run_bash (no venv on PATH → --system → read-only rootfs →
    # pip --user into the wrong interpreter, three turns burned). The remedy
    # must name the EXACT interpreter, and the coordinator prompt must carry
    # the same guide. Keyed to _ENV_REL so a moved env path fails here.
    from adk_cc.sandbox.analysis_env import _ENV_REL
    check("the remedy names the analysis-env interpreter verbatim",
          f"uv pip install --python {_ENV_REL}/bin/python pypdf"
          in hinted["stderr"], hinted["stderr"][-260:])
    check("and points at the durable declaration channels",
          "x-adk-cc/requirements" in hinted["stderr"]
          and "requirements.txt" in hinted["stderr"])
    ro = sk._explain_missing_package(
        {"stdout": "", "stderr": (
            "ModuleNotFoundError: No module named 'pypdf'\n"
            "error: failed to create directory …: Read-only file system"),
         "status": "error"}, loaded["binary-skill"])
    check("a read-only env says do-not-retry, with NO install command",
          "do not retry" in ro["stderr"] and "--python" not in ro["stderr"],
          ro["stderr"][-260:])
    from adk_cc import prompts as _prompts
    check("the coordinator prompt carries the sandbox-runtime guide",
          f"uv pip install --python {_ENV_REL}/bin/python"
          in _prompts.COORDINATOR_INSTRUCTION
          and "x-adk-cc/requirements" in _prompts.COORDINATOR_INSTRUCTION)

    # --- and the bytes reach the script, which is the whole point -------
    check("the binary materialises next to the script that needs it",
          _run_use_sh() == "FOUND", f"got {_run_use_sh()!r}")

    # --- skills installed by ANOTHER agent ------------------------------
    # The implementer guide's cross-client convention: scan `.agents/skills/`
    # alongside your own directory so a skill installed by any compliant agent
    # is visible. Ours wins on a name collision.
    proj = Path(tempfile.mkdtemp(prefix="interop-"))
    for scope, sub in (("mine", ".adk-cc/skills"), ("theirs", ".agents/skills")):
        d = proj / sub / f"{scope}-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {scope}-skill\ndescription: A {scope} skill.\n---\n\nBody.\n")
    both = proj / ".agents" / "skills" / "shared-name"
    both.mkdir(parents=True)
    (both / "SKILL.md").write_text(
        "---\nname: shared-name\ndescription: From the interop dir.\n---\n\nBody.\n")
    mine = proj / ".adk-cc" / "skills" / "shared-name"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text(
        "---\nname: shared-name\ndescription: From adk-cc's own dir.\n---\n\nBody.\n")

    prev = os.environ.pop("ADK_CC_SKILLS_DIR", None)
    # A project's skills load only once the folder is trusted; that gate has its
    # own test, so grant it here and keep this about interop.
    os.environ["ADK_CC_TRUST_PROJECT_SKILLS"] = "1"
    try:
        dirs = [str(d) for d in sk._resolve_skills_dirs(proj)]
        check("the cross-client .agents/skills path is scanned",
              any(d.endswith("/.agents/skills") for d in dirs), dirs[:4])
        sk.clear_project_skill_cache()
        found = {s.frontmatter.name: s
                 for s, _ in sk.discover_skills_with_sources(
                     [p for p in sk._resolve_skills_dirs(proj)])}
        check("a skill another agent installed is usable",
              "theirs-skill" in found, sorted(found))
        check("and adk-cc's own dir still wins a name collision",
              "adk-cc's own" in (found.get("shared-name").frontmatter.description
                                 if "shared-name" in found else ""),
              found.get("shared-name") and found["shared-name"].frontmatter.description)
        check("it is labelled as shared, not as one of ours",
              skill_enablement._classify_source(proj / ".agents" / "skills") == "shared",
              skill_enablement._classify_source(proj / ".agents" / "skills"))
    finally:
        if prev is not None:
            os.environ["ADK_CC_SKILLS_DIR"] = prev
        os.environ.pop("ADK_CC_TRUST_PROJECT_SKILLS", None)
        sk.clear_project_skill_cache()

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
