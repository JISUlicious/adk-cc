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

from .. import deployment

_log = logging.getLogger(__name__)


# Re-exported (kept as the canonical names callers import) — the single readers
# now live in `deployment`.
def desktop_enabled() -> bool:
    return deployment.is_desktop()


def desktop_data_dir() -> Path:
    return deployment.desktop_data_dir()


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

    def _project_root(project_id: str) -> str:
        """Validate a registered project and return its in-place repo root, or 404."""
        if not any(p.get("id") == project_id for p in load_projects()):
            raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")
        repo = project_repo_path(project_id)
        if not repo or not os.path.isdir(repo):
            raise HTTPException(status_code=400, detail="project has no bound repo")
        return repo

    @app.get("/desktop/checkpoint/list", include_in_schema=False)
    async def checkpoint_list(request: Request):  # noqa: ANN202
        q = request.query_params
        project_id = q.get("project_id") or ""
        session_id = q.get("session_id") or ""
        if not project_id or not session_id:
            raise HTTPException(status_code=400, detail="project_id and session_id required")
        _project_root(project_id)  # validate (ignore root here)
        from .desktop_checkpoint import list_checkpoints

        return {"checkpoints": list_checkpoints(project_id, session_id)}

    @app.post("/desktop/checkpoint/restore", include_in_schema=False)
    async def checkpoint_restore(request: Request):  # noqa: ANN202
        body = await request.json() or {}
        project_id = str(body.get("project_id") or "")
        session_id = str(body.get("session_id") or "")
        # Unique checkpoint id (not the git sha, which can repeat). Optional →
        # default: most recent (undo last turn). Accept legacy "sha" as a fallback.
        checkpoint_id = body.get("id") or body.get("sha")
        if not project_id or not session_id:
            raise HTTPException(status_code=400, detail="project_id and session_id required")
        root = _project_root(project_id)
        from .desktop_checkpoint import restore

        result = restore(project_id, session_id, root, checkpoint_id=checkpoint_id or None)
        # Roll the CONVERSATION back to that turn too (files + chat, like a real
        # rewind) — truncate the session's events from the checkpoint's invocation
        # onward. Best-effort: a hiccup here must not fail the (already-done) file
        # restore.
        inv = result.get("invocation_id") if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("status") == "ok" and inv:
            try:
                from .. import deployment
                from .file_session_service import FileSessionService

                fss = FileSessionService(deployment.desktop_data_dir())
                result["events_kept"] = await fss.truncate_before_invocation(
                    user_id=project_id, session_id=session_id, invocation_id=inv
                )
            except Exception:  # noqa: BLE001
                pass
        return result
