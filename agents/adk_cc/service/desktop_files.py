"""Desktop file-tree + file-read routes (ADK_CC_DESKTOP=1).

Read-only view of a session's workspace for the desktop right-side file panel.
In in-place desktop mode that workspace IS the project's repo root, so the panel
shows exactly where the agent works. Two routes, both strictly scoped to that
root via a resolve()-based path guard that rejects any target escaping it (via
``..`` OR a symlink pointing outside). Mounted only when ADK_CC_DESKTOP=1;
desktop is a single-user loopback service (no auth), so these are self-scoped by
project + session id, both validated against the project registry.

Read-only by design: no write/rename/delete. Viewing must NOT create anything,
so it uses ``session_workspace_path`` (non-creating): it returns the bound
project root, or None when no repo is bound.
"""

from __future__ import annotations

import logging
import mimetypes
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

_log = logging.getLogger(__name__)

_MAX_READ = 1024 * 1024  # 1 MiB — cap file reads so the panel can't pull a huge blob
_MAX_ENTRIES = 2000       # per-directory entry cap (keeps huge dirs responsive)
_STATUS_TIMEOUT = 10      # wall-clock cap for the `git status` behind change markers


def _safe(value: str, label: str) -> str:
    """Reject a project/session id that isn't plain alnum/-/_ (defense in depth;
    the real containment guard is _resolve_within's root check)."""
    safe = "".join(c for c in value if c.isalnum() or c in "-_")
    if not safe or safe != value:
        raise HTTPException(status_code=400, detail=f"unsafe {label}: {value!r}")
    return safe


def _resolve_within(project_id: str, session_id: str, rel: str) -> Optional[Path]:
    """Absolute path for ``rel`` inside the session's workspace (in-place: the
    project root).

    Returns None when no project repo is bound. Raises 404 for an unknown
    project, 403 for a path that escapes the workspace root. Both root and target
    are ``.resolve()``d, so ``..`` is collapsed and symlinks are followed before
    the containment check — a symlink inside the workspace pointing outside is
    rejected.
    """
    from .desktop_routes import load_projects
    from .desktop_workspace import session_workspace_path

    project_id = _safe(project_id, "project_id")
    session_id = _safe(session_id, "session_id")
    if not any(p.get("id") == project_id for p in load_projects()):
        raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")

    # Root at the session's actual workspace (in-place: the project root), so the
    # file panel shows exactly where the agent works. Mirrors the tenant resolver.
    ws = session_workspace_path(project_id, session_id)
    if ws is None or not ws.is_dir():
        return None  # no bound project workspace
    root = ws.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=403, detail="path escapes workspace")
    return target


def _coarse_status(xy: str) -> str:
    """Collapse a git porcelain XY status pair into one coarse marker.

    `new` covers untracked (`??`) and staged-add (`A`); `deleted`, `renamed`
    (only when rename detection is on — we run with --no-renames, so a move
    surfaces as delete + new), else `modified` (M/T/C/…). Staged and unstaged
    are merged: a file changed vs HEAD is "changed", regardless of the index.
    """
    if xy == "??":
        return "new"
    x, y = xy[0], xy[1]
    if x == "A" or y == "A":
        return "new"
    if x == "D" or y == "D":
        return "deleted"
    if x == "R" or y == "R":
        return "renamed"
    return "modified"


def _git_working_status(root: Path) -> tuple[bool, dict[str, str]]:
    """`(is_repo, {workspace_rel_path: status})` for the workspace subtree.

    Reads the PROJECT'S OWN working-tree status (the same thing a git client
    shows as uncommitted changes) — the checkpoint shadow git is a separate
    GIT_DIR and is never involved. `status` ∈ {new, modified, deleted,
    renamed}. Paths are workspace-relative with POSIX separators, matching the
    file-tree entries. Best-effort: any failure (not a repo, git missing,
    timeout) yields no markers rather than an error.
    """
    base = ["git", "-C", str(root)]
    try:
        # `--show-prefix` both proves this is a repo AND gives our subdir
        # offset when the workspace root sits below the repo root (git prints
        # status paths relative to the REPO root, so we strip the prefix to
        # get workspace-relative paths).
        pref = subprocess.run(
            base + ["rev-parse", "--show-prefix"],
            capture_output=True,
            text=True,
            timeout=_STATUS_TIMEOUT,
        )
        if pref.returncode != 0:
            return False, {}  # not a git work tree
        prefix = pref.stdout.strip()  # "" at repo root, else "sub/dir/"
        # -z: NUL-delimited, no path quoting. --no-renames: a move shows as
        # D old + ?? new, so every record is a single path (no dual-field
        # rename entries to parse). `-- .` scopes to the workspace subtree.
        res = subprocess.run(
            base
            + [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--no-renames",
                "--",
                ".",
            ],
            capture_output=True,
            text=True,
            timeout=_STATUS_TIMEOUT,
        )
        if res.returncode != 0:
            return True, {}  # a repo, but status failed — no markers
        statuses: dict[str, str] = {}
        for token in res.stdout.split("\0"):
            # Each record is "XY<space>path"; the trailing split yields "".
            if len(token) < 4:
                continue
            xy, path = token[:2], token[3:]
            if prefix:
                if not path.startswith(prefix):
                    continue  # change outside the workspace subtree
                path = path[len(prefix):]
            statuses[path] = _coarse_status(xy)
        return True, statuses
    except (OSError, subprocess.SubprocessError):
        return False, {}


def mount_desktop_files_routes(app) -> None:  # noqa: ANN001
    """Mount /desktop/files/* when ADK_CC_DESKTOP=1; otherwise a no-op."""
    from .desktop_routes import desktop_enabled

    if not desktop_enabled():
        return

    @app.get("/desktop/files/tree", include_in_schema=False)
    async def files_tree(request: Request):  # noqa: ANN202
        q = request.query_params
        project_id = q.get("project_id") or ""
        session_id = q.get("session_id") or ""
        rel = q.get("path") or ""
        if not project_id or not session_id:
            raise HTTPException(status_code=400, detail="project_id and session_id required")

        target = _resolve_within(project_id, session_id, rel)
        if target is None:
            # No project repo bound yet — empty state, not an error.
            return {"root_exists": False, "path": rel, "entries": [], "truncated": False}
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="not a directory")

        entries: list[dict] = []
        truncated = False
        # Dirs first, then files, each case-insensitively sorted.
        for child in sorted(
            target.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())
        ):
            if child.name == ".git":
                continue  # the repo's .git dir is noise in the file panel
            is_dir = child.is_dir()
            try:
                size = None if is_dir else child.stat().st_size
            except OSError:
                size = None
            entries.append(
                {"name": child.name, "type": "dir" if is_dir else "file", "size": size}
            )
            if len(entries) >= _MAX_ENTRIES:
                truncated = True
                break
        return {"root_exists": True, "path": rel, "entries": entries, "truncated": truncated}

    @app.get("/desktop/files/status", include_in_schema=False)
    async def files_status(request: Request):  # noqa: ANN202
        """Whole-workspace git working-tree status → change markers in the
        file panel. One call per reload/turn (git status is a repo-wide op);
        the client looks each tree entry up in the returned map. Empty +
        ``is_repo=false`` when the workspace root isn't a git work tree."""
        q = request.query_params
        project_id = q.get("project_id") or ""
        session_id = q.get("session_id") or ""
        if not project_id or not session_id:
            raise HTTPException(status_code=400, detail="project_id and session_id required")

        root = _resolve_within(project_id, session_id, "")
        if root is None or not root.is_dir():
            return {"is_repo": False, "statuses": {}}
        is_repo, statuses = _git_working_status(root)
        return {"is_repo": is_repo, "statuses": statuses}

    @app.get("/desktop/files/read", include_in_schema=False)
    async def files_read(request: Request):  # noqa: ANN202
        q = request.query_params
        project_id = q.get("project_id") or ""
        session_id = q.get("session_id") or ""
        rel = q.get("path") or ""
        if not project_id or not session_id or not rel:
            raise HTTPException(status_code=400, detail="project_id, session_id, path required")

        target = _resolve_within(project_id, session_id, rel)
        if target is None:
            raise HTTPException(status_code=404, detail="workspace not initialized")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="not a file")

        size = target.stat().st_size
        raw = target.read_bytes()[:_MAX_READ]
        truncated = size > _MAX_READ
        mime, _ = mimetypes.guess_type(target.name)
        try:
            text: Optional[str] = raw.decode("utf-8")
            binary = False
        except UnicodeDecodeError:
            text = None
            binary = True
        return {
            "path": rel,
            "mime": mime or "application/octet-stream",
            "size": size,
            "truncated": truncated,
            "text": text,
            "binary": binary,
        }
