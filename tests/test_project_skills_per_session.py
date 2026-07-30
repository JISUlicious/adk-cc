"""Skill SCOPES: project vs global vs built-in.

Layering (most specific first): `ADK_CC_SKILLS_DIR`, then PROJECT
(`<project>/.adk-cc/skills`, walked up), then GLOBAL (the run dir's
`.adk-cc/skills` and `<desktop data>/skills`), then the built-ins.

Reported from desktop dogfooding: project skills appeared to load from the
global built-ins plus the directory the server was launched from, never from the
bound project root.

Both halves were true. `_resolve_skills_dirs()` walked up from `Path.cwd()` —
the server process's cwd — and the toolset is built ONCE at agent import, so
whatever that walk found was frozen in for every project and every session.

Walk-up behaviour is kept (a skill in a parent monorepo dir is still inherited);
only the anchor changes, from the server's cwd to the session's project root.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_project_skills_per_session.py
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

from adk_cc.tools import skills as sk  # noqa: E402


def _make_skill(root: Path, name: str, body: str = "Do the thing.",
                sub: str = ".adk-cc/skills") -> Path:
    d = root.joinpath(*sub.split("/")) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: >\n  Test skill {name}.\n---\n\n{body}\n",
        encoding="utf-8",
    )
    (d / "references").mkdir(exist_ok=True)
    (d / "references" / "note.md").write_text(f"note for {name}\n", encoding="utf-8")
    return d


def _names(root: str | None) -> set[str]:
    sk.clear_project_skill_cache()
    resolved = sk._skills_for_root(root)
    if resolved is None:
        return set()
    return {s.frontmatter.name for s in resolved[0]}


def test_each_project_sees_its_own_skill() -> None:
    a = Path(tempfile.mkdtemp(prefix="projA-"))
    b = Path(tempfile.mkdtemp(prefix="projB-"))
    _make_skill(a, "alpha-only")
    _make_skill(b, "beta-only")

    names_a, names_b = _names(str(a)), _names(str(b))
    assert "alpha-only" in names_a, sorted(names_a)[:8]
    assert "alpha-only" not in names_b, "project B sees project A's skill"
    assert "beta-only" in names_b
    assert "beta-only" not in names_a, "project A sees project B's skill"
    print("OK each_project_sees_its_own_skill")


def test_built_ins_come_along() -> None:
    """The project layer ADDS; it must not replace the built-in catalogue."""
    a = Path(tempfile.mkdtemp(prefix="projC-"))
    _make_skill(a, "gamma-only")
    names = _names(str(a))
    assert "gamma-only" in names
    assert "data-analyst" in names, "built-ins vanished when a project layer existed"
    print("OK built_ins_come_along")


def test_the_run_dir_is_global_not_the_projects() -> None:
    """The run dir's skills apply to EVERY project — that is what makes them
    global. What must not happen is them arriving as if they were this
    project's, which is how a desktop user ended up with the launch
    directory's skills and none of their own."""
    run_dir = Path(tempfile.mkdtemp(prefix="rundir-"))
    _make_skill(run_dir, "global-from-run-dir")
    p1 = Path(tempfile.mkdtemp(prefix="projD1-"))
    p2 = Path(tempfile.mkdtemp(prefix="projD2-"))
    _make_skill(p1, "delta-only")

    prev = os.getcwd()
    os.chdir(run_dir)                 # as if uvicorn were started here
    try:
        n1, n2 = _names(str(p1)), _names(str(p2))
        assert "delta-only" in n1
        assert "delta-only" not in n2, "a project's skill reached another project"
        # Global reaches both, including the project that has no skills at all.
        assert "global-from-run-dir" in n1 and "global-from-run-dir" in n2
    finally:
        os.chdir(prev)
    print("OK the_run_dir_is_global_not_the_projects")


def test_the_desktop_data_dir_is_global() -> None:
    """`<desktop data>/skills` — e.g. ~/.adk-cc-desktop/skills — applies
    everywhere, so a user can install a skill once for all projects."""
    data = Path(tempfile.mkdtemp(prefix="dataroot-"))
    (data / "skills").mkdir()
    _make_skill(data, "global-from-data-dir", sub="skills")
    project = Path(tempfile.mkdtemp(prefix="projD3-"))

    os.environ["ADK_CC_DATA_DIR"] = str(data)
    try:
        names = _names(str(project))
        assert "global-from-data-dir" in names, sorted(names)[:8]
    finally:
        del os.environ["ADK_CC_DATA_DIR"]
    print("OK the_desktop_data_dir_is_global")


def test_a_claude_skills_folder_is_not_project_scope() -> None:
    """Deliberate: `.claude/skills` used to be accepted as project scope, which
    conflated a Claude Code folder with an adk-cc scope. Only `.adk-cc/skills`
    counts now."""
    project = Path(tempfile.mkdtemp(prefix="projCL-"))
    _make_skill(project, "claude-style", sub=".claude/skills")
    _make_skill(project, "adkcc-style")
    names = _names(str(project))
    assert "adkcc-style" in names
    assert "claude-style" not in names, (
        ".claude/skills is still being read as a project skill source"
    )
    print("OK a_claude_skills_folder_is_not_project_scope")


def test_a_parent_directory_skill_is_still_inherited() -> None:
    """Walk-up is deliberate: a monorepo can put shared skills one level up."""
    mono = Path(tempfile.mkdtemp(prefix="mono-"))
    _make_skill(mono, "shared-by-monorepo")
    child = mono / "packages" / "app"
    child.mkdir(parents=True)
    _make_skill(child, "app-local")
    names = _names(str(child))
    assert {"shared-by-monorepo", "app-local"} <= names, sorted(names)[:8]
    print("OK a_parent_directory_skill_is_still_inherited")


def test_a_project_skill_shadows_a_built_in_by_name() -> None:
    """Documented precedence, unchanged: same name → the project's wins, and
    the other built-ins are untouched."""
    a = Path(tempfile.mkdtemp(prefix="projE-"))
    _make_skill(a, "data-analyst", body="PROJECT OVERRIDE MARKER")
    sk.clear_project_skill_cache()
    resolved = sk._skills_for_root(str(a))
    assert resolved is not None
    by_name = {s.frontmatter.name: s for s in resolved[0]}
    assert "data-analyst" in by_name
    assert "sql-queries" in by_name, "shadowing one built-in hid the rest"
    assert str(a) in resolved[1]["data-analyst"], resolved[1]["data-analyst"]
    print("OK a_project_skill_shadows_a_built_in_by_name")


def test_resources_resolve_to_the_projects_copy() -> None:
    """`load_skill_resource` maps skill name → on-disk dir from an index built
    at construction. A per-session skill is not in that index, so without the
    session-aware lookup its references/ would miss — or hit a same-named
    built-in's files."""
    a = Path(tempfile.mkdtemp(prefix="projF-"))
    d = _make_skill(a, "epsilon-only")
    sk.clear_project_skill_cache()
    token = sk._ACTIVE_PROJECT_ROOT.set(str(a))
    try:
        got = sk._skill_dir_for("epsilon-only", {})
        # resolve() both sides: on macOS the temp dir is /var/... while
        # discovery reports the realpath /private/var/..., which is the same
        # directory and would fail a string/PosixPath comparison.
        assert got is not None and Path(got).resolve() == d.resolve(), (got, d)
        # A name only the base index knows still resolves through the fallback.
        assert sk._skill_dir_for("only-in-base", {"only-in-base": "/base/x"}) == "/base/x"
    finally:
        sk._ACTIVE_PROJECT_ROOT.reset(token)
    print("OK resources_resolve_to_the_projects_copy")


def test_no_root_keeps_the_old_process_wide_behaviour() -> None:
    """Web deployments and `adk web .` have no per-project root; they must keep
    resolving exactly as before rather than losing skills."""
    assert sk._skills_for_root(None) is None
    assert sk._skills_for_root("") is None
    print("OK no_root_keeps_the_old_process_wide_behaviour")


def test_the_opt_out_still_wins() -> None:
    a = Path(tempfile.mkdtemp(prefix="projG-"))
    _make_skill(a, "zeta-only")
    os.environ["ADK_CC_DISABLE_PROJECT_SKILLS"] = "1"
    try:
        sk.clear_project_skill_cache()
        assert sk._skills_for_root(str(a)) is None
    finally:
        del os.environ["ADK_CC_DISABLE_PROJECT_SKILLS"]
    print("OK the_opt_out_still_wins")


def test_the_real_toolset_switches_with_the_active_root() -> None:
    """The wiring, not just the resolver.

    `_skills_for_root` being correct proves nothing about the toolset: ADK's
    lookups (`_list_skills`, `_get_skill`) are context-free accessors on an
    object built ONCE for the agent and shared by every session, which is why
    the bug was frozen-in rather than merely mis-anchored."""
    a = Path(tempfile.mkdtemp(prefix="projH-"))
    b = Path(tempfile.mkdtemp(prefix="projI-"))
    _make_skill(a, "eta-only")
    _make_skill(b, "theta-only")
    sk.clear_project_skill_cache()

    toolset = sk.make_skill_toolset()
    assert toolset is not None

    def listed() -> set[str]:
        return {s.frontmatter.name for s in toolset._list_skills()}

    base = listed()
    assert "eta-only" not in base and "theta-only" not in base, sorted(base)[:8]

    token = sk._ACTIVE_PROJECT_ROOT.set(str(a))
    try:
        names = listed()
        assert "eta-only" in names, sorted(names)[:8]
        assert "theta-only" not in names
        # _get_skill is the accessor load_skill/run_skill_script go through.
        got = toolset._get_skill("eta-only")
        assert got is not None and got.frontmatter.name == "eta-only"
        # A built-in remains reachable from inside a project.
        assert toolset._get_skill("data-analyst") is not None
    finally:
        sk._ACTIVE_PROJECT_ROOT.reset(token)

    token = sk._ACTIVE_PROJECT_ROOT.set(str(b))
    try:
        names = listed()
        assert "theta-only" in names and "eta-only" not in names, sorted(names)[:8]
    finally:
        sk._ACTIVE_PROJECT_ROOT.reset(token)

    # Back to no session → the process-wide set, unchanged.
    assert listed() == base
    print("OK the_real_toolset_switches_with_the_active_root")


def main() -> None:
    test_each_project_sees_its_own_skill()
    test_built_ins_come_along()
    test_the_run_dir_is_global_not_the_projects()
    test_the_desktop_data_dir_is_global()
    test_a_claude_skills_folder_is_not_project_scope()
    test_a_parent_directory_skill_is_still_inherited()
    test_a_project_skill_shadows_a_built_in_by_name()
    test_resources_resolve_to_the_projects_copy()
    test_no_root_keeps_the_old_process_wide_behaviour()
    test_the_opt_out_still_wins()
    test_the_real_toolset_switches_with_the_active_root()
    print("\nall project-skill tests passed")


if __name__ == "__main__":
    main()
