"""Phase 2: TenantSkillToolset unions tenant + user skills (user shadows tenant).

Tests _scoped_skill_sources() — the dir-union the toolset resolves before
building tools: the per-user dir (<root>/<tenant>/_users/<user>/) is scanned in
addition to the tenant dir, users are isolated, and a personal skill shadows a
tenant skill of the same name.

Model-free. Run: .venv/bin/python tests/test_tenant_user_skills.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.tools.skills_tenant import TenantSkillToolset  # noqa: E402


def _mk(d: Path, name: str, marker: str = "x"):
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Skill {name} marker={marker} for the union test.\n---\n\nBody.\n"
    )


def _names(sourced):
    return {sk.frontmatter.name for sk, _ in sourced}


def test_union_user_and_tenant():
    root = tempfile.mkdtemp(prefix="tu-union-")
    _mk(Path(root) / "acme" / "team-skill", "team-skill")
    _mk(Path(root) / "acme" / "_users" / "alice" / "alice-skill", "alice-skill")
    ts = TenantSkillToolset(skill_root=root)

    assert _names(ts._scoped_skill_sources("acme", "alice")) == {"team-skill", "alice-skill"}
    # bob: only the tenant skill (no personal dir)
    assert _names(ts._scoped_skill_sources("acme", "bob")) == {"team-skill"}
    # different tenant: nothing
    assert ts._scoped_skill_sources("beta", "alice") == []


def test_user_only_skill_surfaces():
    # tenant dir has only the _users subtree (no skill of its own) — a personal
    # skill must still surface, proving the user dir is actually scanned.
    root = tempfile.mkdtemp(prefix="tu-solo-")
    _mk(Path(root) / "acme" / "_users" / "alice" / "solo", "solo")
    ts = TenantSkillToolset(skill_root=root)
    assert _names(ts._scoped_skill_sources("acme", "alice")) == {"solo"}
    assert ts._scoped_skill_sources("acme", "bob") == []


def test_user_shadows_tenant_by_name():
    # same skill name in both scopes → the user's wins (its dir is returned)
    root = tempfile.mkdtemp(prefix="tu-shadow-")
    _mk(Path(root) / "acme" / "common", "common", marker="tenant")
    _mk(Path(root) / "acme" / "_users" / "alice" / "common", "common", marker="user")
    ts = TenantSkillToolset(skill_root=root)
    sourced = ts._scoped_skill_sources("acme", "alice")
    assert _names(sourced) == {"common"}  # deduped
    (_, src_dir) = sourced[0]
    assert "_users" in str(src_dir), f"user copy should win, got {src_dir}"


def test_traversal_safe_user_id():
    root = tempfile.mkdtemp(prefix="tu-trav-")
    _mk(Path(root) / "acme" / "team-skill", "team-skill")
    ts = TenantSkillToolset(skill_root=root)
    # an unsafe user_id is skipped (no crash), tenant skill still resolves
    assert _names(ts._scoped_skill_sources("acme", "../escape")) == {"team-skill"}


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__[5:]}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__[5:]}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__[5:]}: {type(e).__name__}: {e}")
    print("\nall tenant-user-skill tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
