"""A project's own skills run only once the user trusts the folder.

Project-scoped skills arrive with the repository. Opening a clone you have not
read used to put its instructions — and its scripts — in front of the agent
immediately. Both the Agent Skills implementer guide and Anthropic's platform
documentation name this risk directly:

    "Consider gating project-level skill loading on a trust check … This
     prevents untrusted repositories from silently injecting instructions into
     the agent's context."

The gate covers PROJECT scope only. Built-ins ship inside adk-cc, global skills
belong to the install, and ADK_CC_SKILLS_DIR is an operator's explicit choice —
none of those arrive with a clone, and gating them would be theatre.

Withholding has to be VISIBLE: a skill that is simply not there is the failure
this whole area keeps producing, so what was skipped is reported by name.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_skill_trust.py
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

_DATA = tempfile.mkdtemp(prefix="trustdata-")
os.environ["ADK_CC_DESKTOP_DATA"] = _DATA
os.environ["ADK_CC_DESKTOP"] = "1"

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _project() -> Path:
    proj = Path(tempfile.mkdtemp(prefix="clone-"))
    for sub, name in ((".adk-cc/skills", "repo-skill"),
                      (".agents/skills", "other-agent-skill")):
        d = proj / sub / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Ships with the repository.\n---\n\nBody.\n")
    return proj


def main() -> int:
    from adk_cc.tools import skill_trust, skills as sk

    proj = _project()
    os.environ.pop("ADK_CC_SKILLS_DIR", None)

    # --- untrusted: withheld, and SAID -----------------------------------
    sk.clear_project_skill_cache()
    dirs = [str(d) for d in sk._resolve_skills_dirs(proj)]
    check("a fresh clone's own skill directory is not scanned",
          not any(str(proj) in d for d in dirs), dirs[:4])
    found = {s.frontmatter.name for s, _ in sk.discover_skills_with_sources(
        sk._resolve_skills_dirs(proj))}
    check("so its skills are not offered to the agent",
          "repo-skill" not in found and "other-agent-skill" not in found,
          sorted(found)[:6])
    withheld = sk.untrusted_project_skills()
    names = withheld.get(str(proj)) or []
    check("but the user is told exactly what was withheld",
          sorted(names) == ["other-agent-skill", "repo-skill"], withheld)
    check("including skills another agent left in the repo",
          "other-agent-skill" in names, names)

    # --- built-ins are unaffected ----------------------------------------
    check("built-in skills still load — they did not come with the clone",
          len(found) > 5, sorted(found)[:6])

    # --- trusting the folder ---------------------------------------------
    skill_trust.set_trusted(proj, True)
    sk.clear_project_skill_cache()
    found = {s.frontmatter.name for s, _ in sk.discover_skills_with_sources(
        sk._resolve_skills_dirs(proj))}
    check("after trusting, the project's skills load",
          "repo-skill" in found, sorted(found)[:6])
    check("and so do the ones another agent installed",
          "other-agent-skill" in found, sorted(found)[:6])
    check("the trust decision persists to disk",
          str(Path(proj).resolve()) in skill_trust.trusted_roots(),
          skill_trust.trusted_roots())

    # --- and can be withdrawn --------------------------------------------
    skill_trust.set_trusted(proj, False)
    sk.clear_project_skill_cache()
    found = {s.frontmatter.name for s, _ in sk.discover_skills_with_sources(
        sk._resolve_skills_dirs(proj))}
    check("withdrawing trust stops them loading again",
          "repo-skill" not in found, sorted(found)[:6])

    # --- a deployment that cannot ask ------------------------------------
    os.environ["ADK_CC_TRUST_PROJECT_SKILLS"] = "1"
    try:
        sk.clear_project_skill_cache()
        found = {s.frontmatter.name for s, _ in sk.discover_skills_with_sources(
            sk._resolve_skills_dirs(proj))}
        check("a headless deployment can opt out of the gate entirely",
              "repo-skill" in found, sorted(found)[:6])
    finally:
        os.environ.pop("ADK_CC_TRUST_PROJECT_SKILLS", None)
        sk.clear_project_skill_cache()

    # --- an unrelated project is not trusted by association --------------
    other = _project()
    check("trust is per folder, not global",
          not sk.skill_trust.is_trusted(other) if hasattr(sk, "skill_trust")
          else not skill_trust.is_trusted(other))

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
