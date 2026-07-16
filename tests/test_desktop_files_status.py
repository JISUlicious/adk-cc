"""Route test for `/desktop/files/status` — the file-panel git change markers.

Exercises the REAL route handler + real `git` against a throwaway repo (no
mocks of git itself), asserting the workspace-relative status map the desktop
file tree consumes. Covers: modified tracked file, untracked new file, a change
in a subdirectory (workspace-relative path), a clean repo (empty map), and a
non-repo workspace (`is_repo=false`).

Run: `uv run python tests/test_desktop_files_status.py`
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
# The route only mounts in desktop mode; force it before importing the module.
os.environ["ADK_CC_DESKTOP"] = "1"


def _git(cwd: str, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t.local",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _client_for(root: Path):
    """A TestClient whose `/desktop/files/*` routes resolve to `root`."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from adk_cc.service import desktop_files, desktop_routes, desktop_workspace

    # `_resolve_within` lazily imports these two, so patching the module
    # attributes is enough to point the routes at our temp workspace.
    desktop_routes.load_projects = lambda: [{"id": "proj1"}]  # type: ignore[assignment]
    desktop_workspace.session_workspace_path = (  # type: ignore[assignment]
        lambda project_id, session_id: root
    )

    app = FastAPI()
    desktop_files.mount_desktop_files_routes(app)
    return TestClient(app)


def _status(root: Path) -> dict:
    client = _client_for(root)
    r = client.get(
        "/desktop/files/status",
        params={"project_id": "proj1", "session_id": "s1"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def test_status_maps_modified_new_and_subdir():
    """A modified tracked file, an untracked new file, and a subdir change all
    map to the right coarse status with workspace-relative POSIX paths."""
    with tempfile.TemporaryDirectory() as d:
        _git(d, "init", "-q")
        (Path(d) / "tracked.txt").write_text("v1", encoding="utf-8")
        (Path(d) / "sub").mkdir()
        (Path(d) / "sub" / "keep.txt").write_text("k1", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "base")

        # Now dirty the tree: modify tracked, add untracked, modify in subdir.
        (Path(d) / "tracked.txt").write_text("v2", encoding="utf-8")
        (Path(d) / "fresh.txt").write_text("new", encoding="utf-8")
        (Path(d) / "sub" / "keep.txt").write_text("k2", encoding="utf-8")

        body = _status(Path(d))
        assert body["is_repo"] is True, body
        st = body["statuses"]
        assert st.get("tracked.txt") == "modified", st
        assert st.get("fresh.txt") == "new", st
        # Subdir path is workspace-relative with a forward slash.
        assert st.get("sub/keep.txt") == "modified", st
        print("OK status_maps_modified_new_and_subdir")


def test_clean_repo_has_empty_map():
    with tempfile.TemporaryDirectory() as d:
        _git(d, "init", "-q")
        (Path(d) / "a.txt").write_text("a", encoding="utf-8")
        _git(d, "add", "-A")
        _git(d, "commit", "-qm", "base")

        body = _status(Path(d))
        assert body["is_repo"] is True, body
        assert body["statuses"] == {}, body
        print("OK clean_repo_has_empty_map")


def test_non_repo_reports_not_a_repo():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "loose.txt").write_text("x", encoding="utf-8")
        body = _status(Path(d))
        assert body["is_repo"] is False, body
        assert body["statuses"] == {}, body
        print("OK non_repo_reports_not_a_repo")


def main():
    test_status_maps_modified_new_and_subdir()
    test_clean_repo_has_empty_map()
    test_non_repo_reports_not_a_repo()
    print("\nall desktop-files-status tests passed")


if __name__ == "__main__":
    main()
