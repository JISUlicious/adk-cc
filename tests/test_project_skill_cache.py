"""Project-skill cache determinism (#107).

The bug was not "the cache is stale" — it was that staleness depended on how
the root PATH happened to be spelled. `_SKILLS_BY_ROOT` was keyed by the raw
string, so one directory could occupy four entries (`/tmp/p`, `/private/tmp/p`,
`/tmp/p/`, `/tmp/p//`), each freezing the skill set as of the first time that
spelling appeared. A user adding a skill saw it appear or not depending on
which spelling that turn presented — the observed live non-determinism.

Two fixes, both pinned here: the key is normalised LEXICALLY (never realpath —
a remote workspace's path must not be resolved against this host), and the
entry is invalidated by a directory signature so adds/removes/edits land on
the next turn with no restart.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_project_skill_cache.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
os.environ.setdefault("ADK_CC_DESKTOP", "1")
os.environ.setdefault("ADK_CC_TRUST_PROJECT_SKILLS", "1")
os.environ.setdefault("ADK_CC_BUILTIN_SKILLS", "0")

from adk_cc.tools import skills as S  # noqa: E402

_passed = _failed = 0
_TMP: list[str] = []


def check(name, ok, detail="") -> None:
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _project() -> Path:
    root = Path(tempfile.mkdtemp(prefix="skcache-"))
    _TMP.append(str(root))
    (root / ".adk-cc" / "skills").mkdir(parents=True)
    S.clear_project_skill_cache()
    # Tests assert on the very next call, faster than any human could act, so
    # the recheck window would mask exactly what they are checking.
    S._SIG_RECHECK_S = 0.0
    return root


def _add(root: Path, name: str, body: str = "body") -> None:
    d = root / ".adk-cc" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(textwrap.dedent(f"""\
        ---
        name: {name}
        description: probe skill {name}
        ---
        {body}
        """))


def _names(root: str) -> list[str]:
    r = S._skills_for_root(root)
    return sorted(getattr(s.frontmatter, "name", "?") for s in (r[0] if r else []))


def test_a_new_skill_appears_without_a_restart() -> None:
    root = _project()
    _add(root, "alpha")
    check("the first skill is discovered", _names(str(root)) == ["alpha"])
    _add(root, "beta")
    check("a skill added later is visible on the next call",
          _names(str(root)) == ["alpha", "beta"], _names(str(root)))


def test_a_removed_skill_disappears() -> None:
    root = _project()
    _add(root, "alpha")
    _add(root, "doomed")
    assert _names(str(root)) == ["alpha", "doomed"]
    shutil.rmtree(root / ".adk-cc" / "skills" / "doomed")
    check("a deleted skill stops being offered",
          _names(str(root)) == ["alpha"], _names(str(root)))


def test_an_edited_manifest_is_repicked_up() -> None:
    root = _project()
    _add(root, "alpha")
    assert _names(str(root)) == ["alpha"]
    # Rename via the frontmatter: the catalogue, not just the body, must move.
    (root / ".adk-cc" / "skills" / "alpha" / "SKILL.md").write_text(
        "---\nname: renamed\ndescription: d\n---\nbody\n")
    check("an edited manifest changes the catalogue",
          _names(str(root)) == ["renamed"], _names(str(root)))


def test_every_spelling_of_one_root_is_one_cache_entry() -> None:
    """THE bug: `/p`, `/p/`, `/p//`, `/p/./` were four entries, each frozen at
    a different moment, so visibility depended on which spelling a turn used."""
    root = _project()
    _add(root, "alpha")
    base = str(root)
    S._SKILLS_BY_ROOT.clear()
    for spelling in (base, base + "/", base + "//", base + "/./", base + "/x/.."):
        _names(spelling)
    check("all spellings collapse to ONE cache entry",
          len(S._SKILLS_BY_ROOT) == 1, sorted(S._SKILLS_BY_ROOT))

    # …and they agree, which is the property the user actually feels.
    _add(root, "beta")
    seen = {sp: tuple(_names(sp)) for sp in (base, base + "/", base + "//")}
    check("and every spelling reports the same skills",
          len(set(seen.values())) == 1 and "beta" in next(iter(seen.values())),
          seen)


def test_the_key_is_never_realpathed() -> None:
    """A remote workspace's path belongs to ANOTHER machine; resolving it
    here is the hazard WorkspaceRoot already refuses to take (#109). On macOS
    /tmp is a symlink to /private/tmp, so realpath would rewrite it."""
    check("normalisation leaves a symlinked path alone",
          S._normalise_root("/tmp/whatever") == "/tmp/whatever",
          S._normalise_root("/tmp/whatever"))
    check("but it still collapses the redundant spellings",
          S._normalise_root("/home/dev/proj//") == "/home/dev/proj"
          and S._normalise_root("/home/dev/./proj") == "/home/dev/proj")


def test_unchanged_dirs_are_not_rediscovered() -> None:
    """The signature has to be an OPTIMISATION, not just a correctness fix:
    if it re-scanned every call it would cost 86x more than it saves."""
    root = _project()
    _add(root, "alpha")
    calls = {"n": 0}
    real = S.discover_skills_with_sources

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    S.discover_skills_with_sources = counting  # type: ignore[assignment]
    try:
        for _ in range(10):
            _names(str(root))
        check("ten calls over an unchanged dir rediscover exactly once",
              calls["n"] == 1, calls["n"])
        _add(root, "beta")
        _names(str(root))
        check("a change triggers exactly one rediscovery", calls["n"] == 2, calls["n"])
    finally:
        S.discover_skills_with_sources = real  # type: ignore[assignment]


def test_a_vanished_project_dir_keeps_serving_the_last_good_set() -> None:
    """A project dir yanked mid-session (unmounted share, deleted folder)
    must not blank the catalogue — losing every skill is worse than serving
    the last known-good one."""
    root = _project()
    _add(root, "alpha")
    assert _names(str(root)) == ["alpha"]
    shutil.rmtree(root)
    check("skills survive the directory disappearing",
          _names(str(root)) == ["alpha"], _names(str(root)))


def test_recheck_window_bounds_the_stat_cost() -> None:
    root = _project()
    _add(root, "alpha")
    S._SIG_RECHECK_S = 60.0
    try:
        _names(str(root))
        sigs = {"n": 0}
        real = S._skills_signature

        def counting(*a, **k):
            sigs["n"] += 1
            return real(*a, **k)

        S._skills_signature = counting  # type: ignore[assignment]
        try:
            for _ in range(20):
                _names(str(root))
            check("inside the window the dirs are not re-stat'ed at all",
                  sigs["n"] == 0, sigs["n"])
        finally:
            S._skills_signature = real  # type: ignore[assignment]
    finally:
        S._SIG_RECHECK_S = 0.0


def test_explicit_clear_still_works() -> None:
    """The Rescan button and the trust endpoint drop the cache outright —
    that path must keep working now that entries are objects, not tuples."""
    root = _project()
    _add(root, "alpha")
    _names(str(root))
    check("the cache holds an entry", len(S._SKILLS_BY_ROOT) == 1)
    S.clear_project_skill_cache()
    check("clear empties it", not S._SKILLS_BY_ROOT)
    check("and discovery still works afterwards", _names(str(root)) == ["alpha"])


def main() -> int:
    for t in (
        test_a_new_skill_appears_without_a_restart,
        test_a_removed_skill_disappears,
        test_an_edited_manifest_is_repicked_up,
        test_every_spelling_of_one_root_is_one_cache_entry,
        test_the_key_is_never_realpathed,
        test_unchanged_dirs_are_not_rediscovered,
        test_a_vanished_project_dir_keeps_serving_the_last_good_set,
        test_recheck_window_bounds_the_stat_cost,
        test_explicit_clear_still_works,
    ):
        print(t.__name__)
        t()
    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
