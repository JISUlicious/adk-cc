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
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

_log = logging.getLogger(__name__)

_MAX_READ = 1024 * 1024  # 1 MiB — cap file reads so the panel can't pull a huge blob
_MAX_ENTRIES = 2000       # per-directory entry cap (keeps huge dirs responsive)


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
