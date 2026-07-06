"""Tests for the desktop file-panel path-escape guard.

`_resolve_within` is the SECURITY BOUNDARY for the read-only file routes: it
must confine every requested path to the session's workspace. In in-place
desktop mode that workspace IS the project's repo root, so these tests pin that
a valid path resolves under the project root, and that `..`, absolute paths, and
symlinks pointing outside the root are all rejected — plus the unknown-project
(404) and no-bound-repo (None) cases.

Run: `.venv/bin/python tests/test_desktop_files_guard.py`
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

# Point the desktop data dir at a throwaway temp dir BEFORE importing anything
# that reads it. desktop_data_dir() reads this env on every call.
_TMP = tempfile.mkdtemp(prefix="adk-cc-files-test-")
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP

from fastapi import HTTPException

from adk_cc.service.desktop_files import _resolve_within

_PROJECT = "proj1"
_SESSION = "sessA"
_NO_REPO_PROJECT = "proj_norepo"


def _setup_project_root() -> Path:
    """Create projects.json + the project's IN-PLACE repo root with a file, a
    subdir, and an outside-pointing symlink. Also registers a second project
    whose repo dir doesn't exist (the no-workspace / None case). Returns the
    project root (== the session workspace)."""
    data = Path(_TMP)
    # The in-place workspace is the project's real repo root.
    root = data / "repo" / _PROJECT
    (data / "projects.json").write_text(
        json.dumps(
            [
                {"id": _PROJECT, "repo_path": str(root)},
                # A registered project whose repo dir doesn't exist → None.
                {"id": _NO_REPO_PROJECT, "repo_path": str(data / "does-not-exist")},
            ]
        ),
        encoding="utf-8",
    )
    (root / "sub").mkdir(parents=True, exist_ok=True)
    (root / "readme.md").write_text("hello", encoding="utf-8")
    (root / "sub" / "inner.txt").write_text("inner", encoding="utf-8")
    # An outside file + a symlink inside the root that points to it.
    outside = data / "OUTSIDE_SECRET.txt"
    outside.write_text("top secret", encoding="utf-8")
    link = root / "escape_link"
    if not link.exists():
        os.symlink(outside, link)
    return root


def test_valid_paths_resolve() -> None:
    root = _setup_project_root()
    assert _resolve_within(_PROJECT, _SESSION, "") == root.resolve()
    assert _resolve_within(_PROJECT, _SESSION, "readme.md") == (root / "readme.md").resolve()
    assert _resolve_within(_PROJECT, _SESSION, "sub") == (root / "sub").resolve()
    assert _resolve_within(_PROJECT, _SESSION, "sub/inner.txt") == (root / "sub" / "inner.txt").resolve()
    print("OK test_valid_paths_resolve")


def _expect_403(rel: str) -> None:
    try:
        _resolve_within(_PROJECT, _SESSION, rel)
    except HTTPException as e:
        assert e.status_code == 403, f"{rel!r} → {e.status_code}, want 403"
        return
    raise AssertionError(f"{rel!r} should have raised 403")


def test_dotdot_escape_blocked() -> None:
    _setup_project_root()
    for rel in ("../../../etc/passwd", "sub/../../..", "../OUTSIDE_SECRET.txt"):
        _expect_403(rel)
    print("OK test_dotdot_escape_blocked")


def test_absolute_path_blocked() -> None:
    _setup_project_root()
    _expect_403("/etc/passwd")
    print("OK test_absolute_path_blocked")


def test_symlink_escape_blocked() -> None:
    _setup_project_root()
    # The symlink lives inside the project root but resolves outside → rejected.
    _expect_403("escape_link")
    print("OK test_symlink_escape_blocked")


def test_unknown_project_404() -> None:
    _setup_project_root()
    try:
        _resolve_within("nope", _SESSION, "")
    except HTTPException as e:
        assert e.status_code == 404, e.status_code
        print("OK test_unknown_project_404")
        return
    raise AssertionError("unknown project should raise 404")


def test_no_bound_repo_returns_none() -> None:
    _setup_project_root()
    # A registered project whose repo dir doesn't exist → None (empty state),
    # for any session id (in-place: session id doesn't create a workspace).
    assert _resolve_within(_NO_REPO_PROJECT, _SESSION, "") is None
    assert _resolve_within(_NO_REPO_PROJECT, "any-session", "") is None
    print("OK test_no_bound_repo_returns_none")


def test_unsafe_ids_rejected() -> None:
    _setup_project_root()
    for bad in ("../etc", "a/b", "a b"):
        try:
            _resolve_within(bad, _SESSION, "")
        except HTTPException as e:
            assert e.status_code in (400, 404), e.status_code
            continue
        raise AssertionError(f"unsafe project id {bad!r} should be rejected")
    print("OK test_unsafe_ids_rejected")


def main() -> None:
    test_valid_paths_resolve()
    test_dotdot_escape_blocked()
    test_absolute_path_blocked()
    test_symlink_escape_blocked()
    test_unknown_project_404()
    test_no_bound_repo_returns_none()
    test_unsafe_ids_rejected()
    print("\nall desktop file-guard tests passed")


if __name__ == "__main__":
    main()
