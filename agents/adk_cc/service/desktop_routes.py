"""Desktop-mode project registry (mounted only when ADK_CC_DESKTOP=1).

A *project* is a local directory the agent works in. Each project maps to a
distinct ADK ``user_id`` (the registry id) — so ADK's per-user session storage
and the per-user credential store give **per-project history + secrets for
free**, and the desktop tenant resolver (P3) maps id → repo → a per-session git
worktree as the workspace.

Registry lives at ``<ADK_CC_DESKTOP_DATA>/projects.json`` (default
``~/.adk-cc-desktop``). No auth: the desktop sidecar runs single-user
(ADK_CC_ALLOW_NO_AUTH=1) on loopback.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

_log = logging.getLogger(__name__)


def desktop_enabled() -> bool:
    return os.environ.get("ADK_CC_DESKTOP") == "1"


def desktop_data_dir() -> Path:
    raw = os.environ.get("ADK_CC_DESKTOP_DATA") or os.path.expanduser("~/.adk-cc-desktop")
    p = Path(os.path.abspath(os.path.expanduser(raw)))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _registry_path() -> Path:
    return desktop_data_dir() / "projects.json"


def load_projects() -> list[dict]:
    p = _registry_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        _log.warning("projects.json unreadable (%s) — treating as empty", e)
        return []


def save_projects(items: list[dict]) -> None:
    _registry_path().write_text(json.dumps(items, indent=2), encoding="utf-8")


def project_repo_path(project_id: str) -> Optional[str]:
    """The on-disk repo for a project id (used by the P3 workspace resolver)."""
    for it in load_projects():
        if it.get("id") == project_id:
            rp = it.get("repo_path")
            return rp if isinstance(rp, str) else None
    return None


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _ensure_git_repo(path: str) -> None:
    """Make `path` a git repo with at least one commit, so per-session worktrees
    can branch from HEAD and contain the project's files. A folder that's already
    a repo is left untouched (worktrees branch from its existing HEAD)."""
    if os.path.isdir(os.path.join(path, ".git")):
        return
    init = _git(["init"], path)
    if init.returncode != 0:
        raise HTTPException(status_code=400, detail=f"git init failed: {init.stderr.strip()}")
    # commit the current contents so HEAD exists (worktrees need a commit).
    _git(["add", "-A"], path)
    _git(
        ["-c", "user.email=adk-cc@local", "-c", "user.name=adk-cc",
         "commit", "--allow-empty", "-m", "adk-cc: initial import"],
        path,
    )


def mount_desktop_routes(app) -> None:
    """Mount /desktop/projects when ADK_CC_DESKTOP=1; otherwise a no-op."""
    if not desktop_enabled():
        return

    @app.get("/desktop/projects", include_in_schema=False)
    async def list_projects():  # noqa: ANN202
        return {"projects": load_projects()}

    @app.post("/desktop/projects", include_in_schema=False)
    async def add_project(request: Request):  # noqa: ANN202
        body = await request.json()
        raw = str((body or {}).get("path") or "").strip()
        if not raw:
            raise HTTPException(status_code=400, detail="'path' required")
        path = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(path):
            raise HTTPException(status_code=400, detail=f"not a directory: {path}")

        items = load_projects()
        existing = next((it for it in items if it.get("repo_path") == path), None)
        if existing:
            return {"project": existing}

        _ensure_git_repo(path)
        proj = {
            "id": uuid.uuid4().hex[:12],
            "name": os.path.basename(path) or path,
            "repo_path": path,
        }
        items.append(proj)
        save_projects(items)
        return {"project": proj}

    @app.delete("/desktop/projects/{project_id}", include_in_schema=False)
    async def remove_project(project_id: str):  # noqa: ANN202
        items = load_projects()
        kept = [it for it in items if it.get("id") != project_id]
        save_projects(kept)
        return {"status": "removed", "id": project_id}

    @app.delete("/desktop/worktree/{project_id}/{session_id}", include_in_schema=False)
    async def remove_session_worktree(project_id: str, session_id: str):  # noqa: ANN202
        # Lazy import breaks the desktop_routes <-> desktop_workspace cycle.
        from .desktop_workspace import remove_worktree

        remove_worktree(project_id, session_id)
        return {"status": "removed"}
