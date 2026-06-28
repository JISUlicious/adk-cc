"""Desktop per-session git worktrees (ADK_CC_DESKTOP=1).

Each chat session works in its own git worktree of its project's repo, so
parallel sessions are isolated working copies. Wired as a custom
``TenancyPlugin`` tenant_resolver: it maps the session's ``user_id`` (= the
project id) → the project repo → a per-session worktree, returned as the
session's ``WorkspaceRoot`` (so run_bash / file tools / skills operate there).

Worktrees live under ``<ADK_CC_DESKTOP_DATA>/worktrees/<project>/<session>``
(under $HOME → the noop backend's prod-path guard passes). Created lazily +
idempotently at a turn's start; removed on session delete (see
``remove_worktree`` + the desktop DELETE route).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..sandbox.workspace import WorkspaceRoot
from .desktop_routes import desktop_data_dir, project_repo_path

_log = logging.getLogger(__name__)


def _worktrees_root() -> Path:
    p = desktop_data_dir() / "worktrees"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _branch(session_id: str) -> str:
    return f"adk-cc/{session_id}"


def ensure_worktree(repo_path: str, project_id: str, session_id: str) -> str:
    """Create (or reuse) the session's worktree; return its absolute path."""
    wt = _worktrees_root() / project_id / session_id
    if (wt / ".git").exists():
        return str(wt)
    wt.parent.mkdir(parents=True, exist_ok=True)
    branch = _branch(session_id)
    # Fresh branch off the project's HEAD.
    r = _git(["worktree", "add", "-b", branch, str(wt)], repo_path)
    if r.returncode != 0:
        # Branch already exists (e.g. a re-created session) → attach to it;
        # stale registration → prune then retry; last resort → detached HEAD.
        _git(["worktree", "prune"], repo_path)
        r2 = _git(["worktree", "add", str(wt), branch], repo_path)
        if r2.returncode != 0:
            r3 = _git(["worktree", "add", "--detach", str(wt)], repo_path)
            if r3.returncode != 0:
                _log.warning("worktree add failed for %s: %s", session_id, r3.stderr.strip())
    return str(wt)


def remove_worktree(project_id: str, session_id: str) -> None:
    """Remove a session's worktree + its branch (best-effort)."""
    wt = _worktrees_root() / project_id / session_id
    repo = project_repo_path(project_id)
    if repo:
        _git(["worktree", "remove", "--force", str(wt)], repo)
        _git(["branch", "-D", _branch(session_id)], repo)
        _git(["worktree", "prune"], repo)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)


@dataclass
class DesktopTenantContext:
    """Minimal tenant context for desktop: workspace() yields the session's
    git worktree (or a flat scratch dir when no project is bound)."""

    tenant_id: str
    user_id: str
    repo_path: Optional[str]

    def workspace(self, session_id: str) -> WorkspaceRoot:
        if self.repo_path and os.path.isdir(self.repo_path):
            abs_path = ensure_worktree(self.repo_path, self.user_id, session_id)
        else:
            # No project bound (default user / pre-project) → a flat scratch dir.
            scratch = desktop_data_dir() / "scratch" / (self.user_id or "local")
            scratch.mkdir(parents=True, exist_ok=True)
            abs_path = str(scratch)
        return WorkspaceRoot(
            tenant_id=self.tenant_id, session_id=session_id, abs_path=abs_path
        )


def desktop_tenant_resolver(user_id: Optional[str]) -> DesktopTenantContext:
    uid = user_id or "local"
    return DesktopTenantContext(tenant_id="local", user_id=uid, repo_path=project_repo_path(uid))
