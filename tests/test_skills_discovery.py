"""Unit tests for project-scope skill auto-discovery.

Covers `_resolve_skills_dirs()` and `discover_skills_with_sources()`:

  - `ADK_CC_SKILLS_DIR` is included first when set.
  - Project walk-up finds `.adk-cc/skills/` in cwd / parents.
  - Per-dir pick-one rule: when both `.adk-cc/skills/` and
    `.claude/skills/` exist in the same dir, `.adk-cc/skills/` wins.
  - `.claude/skills/` is loaded when `.adk-cc/skills/` is absent in
    that dir.
  - `ADK_CC_DISABLE_PROJECT_SKILLS=1` skips the walk-up entirely.
  - Multi-dir aggregation: same skill name in two sources → the
    higher-precedence source wins (first-found dedup).
  - Install fallback (`adk_cc/skills/`) included last and silently
    skipped when absent.

Run: `.venv/bin/python tests/test_skills_discovery.py`
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.tools.skills import (
    _PROJECT_SKILLS_PICK_ONE,
    _resolve_skills_dirs,
    discover_skills_with_sources,
)


# --- Test fixtures -------------------------------------------------


def _write_skill(parent: Path, name: str, description: str = "demo") -> Path:
    """Create a minimal skill at `<parent>/<name>/SKILL.md`. Returns
    the skill dir path."""
    skill_dir = parent / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {name}\n\n"
        "Stub skill body.\n"
    )
    return skill_dir


def _chdir(target: Path):
    """Context manager that chdir's into target, restoring on exit."""

    class _Ctx:
        def __enter__(self_inner):
            self_inner._prev = os.getcwd()
            os.chdir(target)
            return self_inner

        def __exit__(self_inner, *a):
            os.chdir(self_inner._prev)

    return _Ctx()


def _scrub_env(*keys: str):
    """Temporary env scrub — saves originals, deletes, restores on exit."""

    class _Ctx:
        def __enter__(self_inner):
            self_inner._saved = {k: os.environ.get(k) for k in keys}
            for k in keys:
                os.environ.pop(k, None)
            return self_inner

        def __exit__(self_inner, *a):
            for k, v in self_inner._saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    return _Ctx()


# --- _resolve_skills_dirs ----------------------------------------


def test_env_var_skills_dir_wins() -> None:
    """`ADK_CC_SKILLS_DIR` (when set + dir exists) appears first."""
    with tempfile.TemporaryDirectory() as tmp:
        env_dir = Path(tmp) / "env-skills"
        env_dir.mkdir()
        proj_dir = Path(tmp) / "proj"
        (proj_dir / ".adk-cc" / "skills").mkdir(parents=True)
        with _scrub_env("ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"):
            os.environ["ADK_CC_SKILLS_DIR"] = str(env_dir)
            with _chdir(proj_dir):
                dirs = _resolve_skills_dirs()
        # Env var dir first; project dir second.
        assert dirs[0] == env_dir.resolve()
        # Project's .adk-cc/skills is also in the list (just not first).
        assert (proj_dir / ".adk-cc" / "skills").resolve() in dirs
    print("OK test_env_var_skills_dir_wins")


def test_project_walk_up_finds_adk_cc_skills() -> None:
    """Walk-up from cwd surfaces `.adk-cc/skills/` in a parent dir."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        skills_dir = root / ".adk-cc" / "skills"
        skills_dir.mkdir(parents=True)
        sub = root / "src" / "deep"
        sub.mkdir(parents=True)
        with _scrub_env("ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"):
            with _chdir(sub):
                dirs = _resolve_skills_dirs()
        assert skills_dir.resolve() in dirs
    print("OK test_project_walk_up_finds_adk_cc_skills")


def test_pick_one_adk_cc_priority_over_claude() -> None:
    """When BOTH `.adk-cc/skills/` and `.claude/skills/` exist in the
    same dir, only `.adk-cc/skills/` is included. The pick-one rule
    prevents double-registration of identical skills."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        adk_skills = root / ".adk-cc" / "skills"
        claude_skills = root / ".claude" / "skills"
        adk_skills.mkdir(parents=True)
        claude_skills.mkdir(parents=True)
        with _scrub_env("ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"):
            with _chdir(root):
                dirs = _resolve_skills_dirs()
        assert adk_skills.resolve() in dirs
        assert claude_skills.resolve() not in dirs
    print("OK test_pick_one_adk_cc_priority_over_claude")


def test_claude_skills_loaded_when_adk_cc_absent() -> None:
    """In a dir with only `.claude/skills/` (no `.adk-cc/skills/`),
    `.claude/skills/` is loaded — the fallback half of pick-one."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        claude_skills = root / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        with _scrub_env("ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"):
            with _chdir(root):
                dirs = _resolve_skills_dirs()
        assert claude_skills.resolve() in dirs
    print("OK test_claude_skills_loaded_when_adk_cc_absent")


def test_disable_env_var_skips_walk_up() -> None:
    """`ADK_CC_DISABLE_PROJECT_SKILLS=1` → walk-up disabled. Project
    skills are NOT in the resolved list even when they exist."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        skills_dir = root / ".adk-cc" / "skills"
        skills_dir.mkdir(parents=True)
        with _scrub_env("ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"):
            os.environ["ADK_CC_DISABLE_PROJECT_SKILLS"] = "1"
            with _chdir(root):
                dirs = _resolve_skills_dirs()
        assert skills_dir.resolve() not in dirs
    print("OK test_disable_env_var_skips_walk_up")


def test_per_dir_decisions_are_independent() -> None:
    """Parent has only `.claude/skills/`; child has only
    `.adk-cc/skills/`. Walk-up emits CHILD's `.adk-cc/skills/` and
    PARENT's `.claude/skills/` — pick-one is per directory, not
    global."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        parent_claude = root / ".claude" / "skills"
        parent_claude.mkdir(parents=True)
        child = root / "sub"
        child_adk = child / ".adk-cc" / "skills"
        child_adk.mkdir(parents=True)
        with _scrub_env("ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"):
            with _chdir(child):
                dirs = _resolve_skills_dirs()
        assert child_adk.resolve() in dirs
        assert parent_claude.resolve() in dirs
        # Child's dir appears BEFORE parent's (walked first).
        assert dirs.index(child_adk.resolve()) < dirs.index(parent_claude.resolve())
    print("OK test_per_dir_decisions_are_independent")


def test_missing_dirs_silently_skipped() -> None:
    """A project with no skills dirs at all → resolution returns only
    the install fallback (if it exists) or empty."""
    with tempfile.TemporaryDirectory() as tmp:
        with _scrub_env("ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"):
            with _chdir(tmp):
                dirs = _resolve_skills_dirs()
        # Whatever's in the list must be REAL dirs.
        for d in dirs:
            assert d.is_dir(), d
    print("OK test_missing_dirs_silently_skipped")


# --- discover_skills_with_sources --------------------------------


def test_aggregate_first_source_wins_on_duplicate_names() -> None:
    """Same skill name in two dirs → the higher-precedence source
    wins. With env-var dir first, its `greeter` overrides the
    project's `greeter`."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        env_skills = root / "env-skills"
        env_skills.mkdir()
        _write_skill(env_skills, "greeter", description="env-version")

        proj = root / "proj"
        proj_skills = proj / ".adk-cc" / "skills"
        proj_skills.mkdir(parents=True)
        _write_skill(proj_skills, "greeter", description="project-version")
        _write_skill(proj_skills, "linter", description="project-only")

        with _scrub_env("ADK_CC_SKILLS_DIR", "ADK_CC_DISABLE_PROJECT_SKILLS"):
            os.environ["ADK_CC_SKILLS_DIR"] = str(env_skills)
            with _chdir(proj):
                pairs = discover_skills_with_sources()
        names = [s.frontmatter.name for s, _ in pairs]
        assert "greeter" in names
        assert "linter" in names
        # The greeter pair should be the ENV one (env-version), NOT
        # the project one. Inspect its source path.
        greeter_pair = next((s, d) for (s, d) in pairs if s.frontmatter.name == "greeter")
        _, greeter_dir = greeter_pair
        assert env_skills.resolve() in greeter_dir.parents or greeter_dir.parent == env_skills.resolve()
    print("OK test_aggregate_first_source_wins_on_duplicate_names")


def test_discover_with_explicit_dirs_argument() -> None:
    """Caller can pass `skills_dirs` directly to bypass resolution
    (useful for tests; also lets ops construct custom orderings)."""
    with tempfile.TemporaryDirectory() as tmp:
        d1 = Path(tmp) / "one"
        d1.mkdir()
        _write_skill(d1, "alpha")
        d2 = Path(tmp) / "two"
        d2.mkdir()
        _write_skill(d2, "beta")
        pairs = discover_skills_with_sources(skills_dirs=[d1, d2])
        names = sorted(s.frontmatter.name for s, _ in pairs)
        assert names == ["alpha", "beta"]
    print("OK test_discover_with_explicit_dirs_argument")


def test_aggregate_returns_skill_source_pairs() -> None:
    """Output is `(skill, source_dir)` pairs — caller can build a
    name→path index regardless of which root contributed."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "skills"
        d.mkdir()
        skill_dir = _write_skill(d, "my-skill")
        pairs = discover_skills_with_sources(skills_dirs=[d])
        assert len(pairs) == 1
        skill, source = pairs[0]
        assert skill.frontmatter.name == "my-skill"
        assert source.resolve() == skill_dir.resolve()
    print("OK test_aggregate_returns_skill_source_pairs")


def test_pick_one_constant_priority_order() -> None:
    """The pick-one constant declares `.adk-cc/skills` first. Tests
    pin the priority so a re-order would surface as a behavior
    change rather than a silent regression."""
    assert _PROJECT_SKILLS_PICK_ONE == (".adk-cc/skills", ".claude/skills")
    print("OK test_pick_one_constant_priority_order")


# --- Driver -------------------------------------------------------


def main() -> None:
    test_env_var_skills_dir_wins()
    test_project_walk_up_finds_adk_cc_skills()
    test_pick_one_adk_cc_priority_over_claude()
    test_claude_skills_loaded_when_adk_cc_absent()
    test_disable_env_var_skips_walk_up()
    test_per_dir_decisions_are_independent()
    test_missing_dirs_silently_skipped()
    test_aggregate_first_source_wins_on_duplicate_names()
    test_discover_with_explicit_dirs_argument()
    test_aggregate_returns_skill_source_pairs()
    test_pick_one_constant_priority_order()
    print("\nall skills-discovery tests passed")


if __name__ == "__main__":
    main()
