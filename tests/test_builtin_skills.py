"""W2: built-in skills (the `<install>/adk_cc/skills/` base layer).

Properties pinned here:
  - built-ins load with no project skills configured;
  - a project skill of the SAME NAME overrides the built-in, and does not
    hide the other built-ins (first-found wins per name, not per directory);
  - `ADK_CC_BUILTIN_SKILLS=0` removes the whole layer;
  - no duplicate-name crash when a name exists in two sources;
  - the `list_skills` catalog stays within a token budget (the real scarce
    resource — skills are progressively disclosed, so the cost is per-call
    catalog size, not per-skill tool schemas);
  - **packaging**: a built wheel actually CONTAINS the SKILL.md files.
    `[tool.setuptools.packages.find]` ships only *.py, so without explicit
    package-data every installed copy would silently have zero skills.

Run: `uv run python tests/test_builtin_skills.py`
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
# Project walk-up would otherwise pick up this repo's own .claude/skills.
os.environ.setdefault("ADK_CC_DISABLE_PROJECT_SKILLS", "1")

from adk_cc.tools.skills import (  # noqa: E402
    _resolve_skills_dirs,
    discover_skills_with_sources,
)

_REPO = Path(__file__).resolve().parent.parent
_BUILTIN_DIR = _REPO / "agents" / "adk_cc" / "skills"


def _names():
    return {s.name for s, _ in discover_skills_with_sources()}


def test_builtins_load_by_default():
    names = _names()
    assert "data-analyst" in names, names
    dirs = [str(d) for d in _resolve_skills_dirs()]
    assert any(d.endswith("adk_cc/skills") for d in dirs), dirs
    print("OK builtins_load_by_default")


def test_builtin_has_frontmatter_and_references():
    skills = {s.name: s for s, _ in discover_skills_with_sources()}
    s = skills["data-analyst"]
    assert s.description and len(s.description) > 40, s.description
    refs = getattr(getattr(s, "resources", None), "references", None) or {}
    # the methodology companions must be reachable, not just SKILL.md
    assert len(refs) >= 10, list(refs)[:5]
    assert any("root-cause" in k for k in refs), list(refs)[:5]
    print(f"OK builtin_has_frontmatter_and_references ({len(refs)} refs)")


def test_kill_switch_removes_the_layer():
    os.environ["ADK_CC_BUILTIN_SKILLS"] = "0"
    try:
        dirs = [str(d) for d in _resolve_skills_dirs()]
        assert not any(d.endswith("adk_cc/skills") for d in dirs), dirs
        assert "data-analyst" not in _names()
    finally:
        os.environ.pop("ADK_CC_BUILTIN_SKILLS", None)
    assert "data-analyst" in _names(), "layer must come back"
    print("OK kill_switch_removes_the_layer")


def test_project_skill_overrides_by_name_only():
    """A same-named project skill wins; the OTHER built-ins survive. This is
    the property that makes built-ins safe to ship — users override one
    without losing the base layer."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "data-analyst"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: data-analyst\ndescription: LOCAL OVERRIDE of the "
            "built-in data analyst, used to prove precedence.\n---\n\nlocal\n"
        )
        os.environ["ADK_CC_SKILLS_DIR"] = tmp
        try:
            skills = {s.name: s for s, _ in discover_skills_with_sources()}
            assert "LOCAL OVERRIDE" in (skills["data-analyst"].description or ""), \
                skills["data-analyst"].description
            # no duplicate-name crash, exactly one entry for the name
            names = [s.name for s, _ in discover_skills_with_sources()]
            assert names.count("data-analyst") == 1, names
        finally:
            os.environ.pop("ADK_CC_SKILLS_DIR", None)
    # …and the built-in is back once the override goes away
    skills = {s.name: s for s, _ in discover_skills_with_sources()}
    assert "LOCAL OVERRIDE" not in (skills["data-analyst"].description or "")
    print("OK project_skill_overrides_by_name_only")


def test_catalog_token_budget():
    """Guard against skill sprawl: the catalog is paid on every `list_skills`
    call, and selection precision degrades before tokens do."""
    pairs = discover_skills_with_sources()
    payload = "\n".join(
        f"<skill><name>{s.name}</name><description>{s.description}</description></skill>"
        for s, _ in pairs
    )
    approx_tokens = len(payload) // 4
    assert approx_tokens < 2000, f"catalog ~{approx_tokens} tokens for {len(pairs)} skills"
    print(f"OK catalog_token_budget (~{approx_tokens} tokens, {len(pairs)} skills)")


def test_wheel_contains_skill_files():
    """REAL build. `packages.find` ships only *.py — without package-data the
    wheel would contain zero SKILL.md and every installed copy would have an
    empty built-in layer while passing all the tests above."""
    import shutil

    builder = (["uv", "build", "--wheel", "--out-dir"] if shutil.which("uv")
               else [sys.executable, "-m", "build", "--wheel", "--outdir"])
    with tempfile.TemporaryDirectory() as out:
        proc = subprocess.run(
            builder + [out, str(_REPO)], capture_output=True, text=True, timeout=900,
        )
        assert proc.returncode == 0, (
            "wheel build failed — packaging cannot be left unverified:\n"
            + (proc.stderr or proc.stdout)[-400:]
        )
        wheels = list(Path(out).glob("*.whl"))
        assert wheels, "no wheel produced"
        with zipfile.ZipFile(wheels[0]) as z:
            names = z.namelist()
        skill_md = [n for n in names if n.endswith("skills/data-analyst/SKILL.md")]
        refs = [n for n in names if "skills/data-analyst/references/" in n]
        assert skill_md, "wheel has no built-in SKILL.md — package-data missing"
        assert len(refs) >= 10, f"wheel has {len(refs)} reference files"
        print(f"OK wheel_contains_skill_files (SKILL.md + {len(refs)} references)")


def main():
    test_builtins_load_by_default()
    test_builtin_has_frontmatter_and_references()
    test_kill_switch_removes_the_layer()
    test_project_skill_overrides_by_name_only()
    test_catalog_token_budget()
    test_wheel_contains_skill_files()
    print("\nall built-in skill tests passed")


if __name__ == "__main__":
    main()
