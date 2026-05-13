"""Inner script — runs the discovery scenarios with seeded files."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Importing adk_cc.agent applies env-driven logging. Don't import here
# so we don't trip the install-dir-fallback path with skills already
# present in adk_cc/skills/. We import on demand in scenario 5.

from adk_cc.tools.skills import (
    _resolve_skills_dirs,
    discover_skills_with_sources,
    make_skill_toolset,
)


# --- Fixtures ------------------------------------------------------


def _seed_skill(parent: Path, name: str, description: str = "demo") -> Path:
    """Create a minimal skill at <parent>/<name>/SKILL.md."""
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {name}\n\n"
        "Stub skill body for the demo.\n"
    )
    return skill_dir


class _ChDir:
    def __init__(self, target: Path) -> None:
        self.target = target

    def __enter__(self):
        self._prev = os.getcwd()
        os.chdir(self.target)
        return self

    def __exit__(self, *a):
        os.chdir(self._prev)


class _ScrubEnv:
    def __init__(self, *keys: str) -> None:
        self.keys = keys

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self.keys}
        for k in self.keys:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _summary(label: str, dirs: list[Path]) -> None:
    """Print resolved dirs, marking the install-fallback so the demo
    output stays readable."""
    print(f"  resolved {len(dirs)} dir(s):")
    install_marker = (Path(__file__).resolve().parent.parent / "adk_cc" / "skills").resolve()
    for d in dirs:
        suffix = "  (install fallback)" if d == install_marker else ""
        print(f"    - {d}{suffix}")


# --- Scenarios -----------------------------------------------------


def scenario_1_project_only() -> None:
    print("\n[scenario 1] project-only `.adk-cc/skills/`")
    print("  expect: discovery finds it without ADK_CC_SKILLS_DIR")
    with tempfile.TemporaryDirectory() as tmp, _ScrubEnv(
        "ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"
    ), _ChDir(Path(tmp)):
        _seed_skill(Path(tmp) / ".adk-cc" / "skills", "greeter")
        dirs = _resolve_skills_dirs()
        _summary("dirs", dirs)
        pairs = discover_skills_with_sources()
        names = [s.frontmatter.name for s, _ in pairs]
        print(f"  loaded skills: {names}")
        assert "greeter" in names, names


def scenario_2_pick_one_adk_cc_wins() -> None:
    print("\n[scenario 2] both `.adk-cc/skills/` AND `.claude/skills/` in same dir")
    print("  expect: only `.adk-cc/skills/` is in the resolved list")
    with tempfile.TemporaryDirectory() as tmp, _ScrubEnv(
        "ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"
    ), _ChDir(Path(tmp)):
        _seed_skill(Path(tmp) / ".adk-cc" / "skills", "from-adk-cc")
        _seed_skill(Path(tmp) / ".claude" / "skills", "from-claude")
        dirs = _resolve_skills_dirs()
        _summary("dirs", dirs)
        pairs = discover_skills_with_sources()
        names = [s.frontmatter.name for s, _ in pairs]
        print(f"  loaded skills: {names}")
        assert "from-adk-cc" in names, names
        assert "from-claude" not in names, names


def scenario_3_claude_fallback() -> None:
    print("\n[scenario 3] only `.claude/skills/` (no `.adk-cc/skills/`)")
    print("  expect: `.claude/skills/` loaded as fallback")
    with tempfile.TemporaryDirectory() as tmp, _ScrubEnv(
        "ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"
    ), _ChDir(Path(tmp)):
        _seed_skill(Path(tmp) / ".claude" / "skills", "from-claude-only")
        dirs = _resolve_skills_dirs()
        _summary("dirs", dirs)
        pairs = discover_skills_with_sources()
        names = [s.frontmatter.name for s, _ in pairs]
        print(f"  loaded skills: {names}")
        assert "from-claude-only" in names, names


def scenario_4_env_var_shadows_project() -> None:
    print("\n[scenario 4] same skill name in env dir + project dir")
    print("  expect: env wins; project's `greeter` shadowed and logged at INFO")
    with tempfile.TemporaryDirectory() as tmp, _ScrubEnv(
        "ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"
    ):
        env_skills = Path(tmp) / "env-skills"
        env_skills.mkdir()
        _seed_skill(env_skills, "greeter", description="env-version (wins)")
        proj = Path(tmp) / "proj"
        proj.mkdir()
        proj_skills_dir = _seed_skill(
            proj / ".adk-cc" / "skills", "greeter", description="project-version (loses)"
        )
        os.environ["ADK_CC_SKILLS_DIR"] = str(env_skills)
        with _ChDir(proj):
            dirs = _resolve_skills_dirs()
            _summary("dirs", dirs)
            pairs = discover_skills_with_sources()
            names = [s.frontmatter.name for s, _ in pairs]
            print(f"  loaded skills (dedup'd): {names}")
            # Print which version actually won by inspecting the source dir.
            for s, src in pairs:
                if s.frontmatter.name == "greeter":
                    won = "ENV" if env_skills.resolve() == src.parent else "PROJECT"
                    print(f"  greeter winner: {won} (source: {src})")
                    print(f"  description:    {s.frontmatter.description}")


def scenario_5_full_toolset_boot() -> None:
    print("\n[scenario 5] full `make_skill_toolset()` boot")
    print("  expect: toolset non-None; dispatch tools wired; project skill")
    print("          appears in the toolset's skill registry")
    with tempfile.TemporaryDirectory() as tmp, _ScrubEnv(
        "ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"
    ), _ChDir(Path(tmp)):
        _seed_skill(Path(tmp) / ".adk-cc" / "skills", "demo-skill")
        toolset = make_skill_toolset()
        if toolset is None:
            print("  toolset is None — discovery returned no skills")
            return
        tools = getattr(toolset, "_tools", [])
        tool_names = [getattr(t, "name", repr(t)) for t in tools]
        print(f"  dispatch tools ({len(tools)}):")
        for n in tool_names:
            print(f"    - {n}")

        # The actual skill loads INTO the toolset's registry. ADK's
        # SkillToolset stores them on `_skills` (dict) and exposes via
        # `_list_skills()`. The dispatch tools above use this registry
        # to enumerate / load / run skills.
        skills = toolset._list_skills()
        skill_names = [getattr(s, "name", None) or s.frontmatter.name for s in skills]
        print(f"  skills registered in toolset ({len(skills)}):")
        for n in skill_names:
            print(f"    - {n}")
        assert "demo-skill" in skill_names, skill_names


# --- Driver --------------------------------------------------------


def main() -> int:
    print("=" * 70)
    print("Project skills auto-discovery demo")
    print("=" * 70)
    scenario_1_project_only()
    scenario_2_pick_one_adk_cc_wins()
    scenario_3_claude_fallback()
    scenario_4_env_var_shadows_project()
    scenario_5_full_toolset_boot()
    print("\n[demo] complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
