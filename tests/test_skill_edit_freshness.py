"""An edited skill SCRIPT must reach the next run without any reload.

Reported live from web mode: bind-mounted edits reached the container but
runs kept executing the OLD script — and web has no reload button. Root
cause: ADK read_text()s scripts/references/assets ONCE into SkillResources
dicts, and _skills_signature only fingerprinted SKILL.md, on the false
premise that bodies are read lazily. Editing scripts/foo.py moved nothing,
the cached Skill objects kept old content, the materialisation digest never
changed. Desktop's explicit reload merely masked the same bug.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_skill_edit_freshness.py
"""
from __future__ import annotations

import os, sys, tempfile, textwrap, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
os.environ.setdefault("ADK_CC_TRUST_PROJECT_SKILLS", "1")

_passed = _failed = 0
def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok: _passed += 1
    else: _failed += 1

def main() -> int:
    from adk_cc.tools import skills as S

    proj = tempfile.mkdtemp(prefix="fresh-")
    d = Path(proj, ".adk-cc", "skills", "greeter", "scripts")
    d.mkdir(parents=True)
    (d.parent / "SKILL.md").write_text(textwrap.dedent("""\
        ---
        name: greeter
        description: greets
        ---
        run scripts/greet.py
        """))
    (d / "greet.py").write_text("print('OLD-OUTPUT')\n")

    def load():
        # _skills_for_root -> (list[Skill], {name: dir}); objects live in [0].
        skills, _dirs = S._skills_for_root(proj)
        return next(s for s in skills
                    if getattr(s.frontmatter, "name", "") == "greeter")

    s1 = load()
    files1 = S._skill_files(s1)
    d1 = S._files_digest(files1)
    check("cold load sees the old script", "OLD-OUTPUT" in str(files1.get("scripts/greet.py")))

    # Edit the script ON DISK, no reload of any kind. Bump mtime past the
    # signature TTL and filesystem mtime granularity.
    time.sleep(1.1)
    (d / "greet.py").write_text("print('NEW-OUTPUT')\n")

    s2 = load()
    files2 = S._skill_files(s2)
    d2 = S._files_digest(files2)
    check("the edit is visible on the next access (no reload)",
          "NEW-OUTPUT" in str(files2.get("scripts/greet.py")),
          "cached Skill object served stale script content")
    check("the materialisation digest changed with it", d1 != d2,
          f"{d1} == {d2} — the old runtime copy would keep running")

    # A NESTED file must count too — ADK loads by relative path.
    time.sleep(1.1)
    lib = d / "lib"; lib.mkdir(exist_ok=True)
    (lib / "util.py").write_text("X = 1\n")
    s3 = load(); d3 = S._files_digest(S._skill_files(s3))
    check("adding a nested script file re-materialises", d3 != d2)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0

if __name__ == "__main__":
    sys.exit(main())
