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


def project_remote(project_id: str) -> Optional[dict]:
    """The remote (SSH) binding for a project id — `{"host", "path", "port"?}`
    — or None for a local project. A project is EITHER local (repo_path) or
    remote; the workspace resolver + backend factory branch on this."""
    for it in load_projects():
        if it.get("id") == project_id:
            r = it.get("remote")
            if isinstance(r, dict) and r.get("host") and r.get("path"):
                return r
            return None
    return None


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _shq_remote(s: str) -> str:
    """POSIX single-quoting for a path embedded in a remote sh command."""
    import shlex

    return shlex.quote(s)


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

    @app.get("/desktop/sessions/backend", include_in_schema=False)
    async def session_backend(request: Request):  # noqa: ANN202
        """The RESOLVED sandbox backend for one session — the truth the
        composer badge shows. `source="live"` once the session has run a
        turn (TenancyPlugin noted the actual backend object);
        `source="config"` before that (what a new chat WOULD get). The
        global settings endpoint can diverge from this — per-session
        overrides, container→host fallback, per-project SSH — which is
        exactly why this exists."""
        q = request.query_params
        session_id = q.get("session_id") or ""
        project_id = q.get("project_id") or ""
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id required")

        from ..sandbox import (
            backend_public_info,
            is_isolated_backend_name,
            resolved_session_backend,
        )

        b = resolved_session_backend(session_id)
        if b is not None:
            return {"source": "live", **backend_public_info(b)}
        # Config prediction. A REMOTE project resolves to ssh regardless of
        # the global backend setting — predict that so the badge is right
        # before the first turn too.
        if project_id:
            r = project_remote(project_id)
            if r:
                return {
                    "source": "config",
                    "backend": "ssh",
                    "detail": r["host"],
                    "isolated": False,
                }
        name = deployment.sandbox_backend_name()
        out: dict = {
            "source": "config",
            "backend": name,
            "detail": None,
            "isolated": is_isolated_backend_name(name),
        }
        if name == "container":
            # The config-level nuance the old badge showed: opted in but no
            # runtime → commands would fall back to the host.
            out["available"] = deployment.container_runtime_available()
        return out

    @app.post("/desktop/projects/remote", include_in_schema=False)
    async def add_remote_project(request: Request):  # noqa: ANN202
        """Register a REMOTE (SSH) project: `{host, path, name?, port?}`.
        `host` is anything the user's `ssh` accepts (alias/user@host); `path`
        is the ABSOLUTE workspace root on the remote. Creation does NOT
        require the host to be reachable (offline-tolerant) — use
        /desktop/projects/test-remote for the connection check."""
        body = await request.json() or {}
        host = str(body.get("host") or "").strip()
        path = str(body.get("path") or "").strip().rstrip("/")
        if not host or not path:
            raise HTTPException(status_code=400, detail="'host' and 'path' required")
        if not path.startswith("/"):
            raise HTTPException(
                status_code=400, detail="'path' must be an absolute remote path"
            )
        port = body.get("port")
        try:
            port = int(port) if port not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="'port' must be an int")

        items = load_projects()
        existing = next(
            (
                it
                for it in items
                if isinstance(it.get("remote"), dict)
                and it["remote"].get("host") == host
                and it["remote"].get("path") == path
            ),
            None,
        )
        if existing:
            return {"project": existing}
        remote: dict = {"host": host, "path": path}
        if port:
            remote["port"] = port
        proj = {
            "id": uuid.uuid4().hex[:12],
            "name": str(body.get("name") or "").strip()
            or f"{host}:{os.path.basename(path) or path}",
            "remote": remote,
        }
        items.append(proj)
        save_projects(items)
        return {"project": proj}

    @app.post("/desktop/projects/test-remote", include_in_schema=False)
    async def test_remote_project(request: Request):  # noqa: ANN202
        """Connection test for a remote binding: probes over the SAME transport
        the backend will use (key/agent auth, BatchMode — never prompts).
        Returns `{ok, home, git, uname}` or `{ok: false, error}` with the
        transport's actionable message ("run `ssh <host>` once first…")."""
        body = await request.json() or {}
        host = str(body.get("host") or "").strip()
        if not host:
            raise HTTPException(status_code=400, detail="'host' required")
        port = body.get("port")
        try:
            port = int(port) if port not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="'port' must be an int")

        from ..sandbox.ssh_transport import SshConnectionError, get_transport

        t = get_transport(host, port=port)
        try:
            probe = await t.probe(refresh=True, timeout_s=20)
        except SshConnectionError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 — surface, don't 500
            return {"ok": False, "error": f"probe failed: {e}"}
        out = {"ok": True, **probe}
        path = str(body.get("path") or "").strip()
        if path:
            res = await t.run(f"[ -d {_shq_remote(path)} ] && echo D || echo N")
            out["path_exists"] = res.stdout.strip() == "D"
        return out

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
        # Also reap the session's sandbox container (deterministic teardown for
        # the container backend; no-op for host exec). Off the loop; best-effort.
        try:
            import asyncio as _asyncio

            from ..sandbox.backends.local_container_backend import remove_session_container

            await _asyncio.to_thread(remove_session_container, session_id)
        except Exception:  # noqa: BLE001 — never block a delete on cleanup
            pass
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
