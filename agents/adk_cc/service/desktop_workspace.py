"""Desktop workspace resolver (ADK_CC_DESKTOP=1).

Wired as a custom ``TenancyPlugin`` tenant_resolver: it maps the session's
``user_id`` (= the project id) → the project repo, returned as the session's
``WorkspaceRoot`` (so run_bash / file tools / skills operate there).

Default is **in-place**: every session of a project works directly in that
project's repo root, so the agent edits the user's real files (Claude Code /
Hermes style), with the checkpoint plugin as the undo net. adk-cc has no
parallel subagent workers, so per-session isolation earns nothing here.

The git-worktree helpers below (``ensure_worktree`` / ``remove_worktree`` /
``session_worktree_path``, living under
``<ADK_CC_DESKTOP_DATA>/worktrees/<project>/<session>``) are **retained but
dormant** — no longer on the desktop path, reserved for a future "isolate this
session" toggle or if concurrent/background workers are added.
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


def session_worktree_path(project_id: str, session_id: str) -> Path:
    """The session's worktree path WITHOUT creating it — for read-only viewing.

    Unlike ``ensure_worktree``, this never spawns a git worktree: a brand-new
    session has no worktree until its first turn, and merely *viewing* the file
    panel must not create one. Callers check ``.is_dir()`` to detect the
    not-yet-initialized case."""
    return _worktrees_root() / project_id / session_id


def session_workspace_path(project_id: str, session_id: str) -> Optional[Path]:
    """The directory a session's agent actually works in — for read-only viewing
    (the file panel), so the panel and the agent point at the *same* dir by
    construction. In-place desktop: the project's repo root. Returns None if the
    project has no bound repo. Never creates anything. `session_id` is unused
    today (kept for a future per-session isolate mode, which would return the
    worktree)."""
    repo = project_repo_path(project_id)
    if repo and os.path.isdir(repo):
        return Path(repo)
    return None


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
    """Remove a session's worktree + its branch (best-effort). No-op in in-place
    mode (no worktree exists)."""
    wt = _worktrees_root() / project_id / session_id
    # Safety: never let a crafted id escape the worktrees root — the rmtree below
    # must ONLY ever touch a path under worktrees/, never e.g. the project itself.
    try:
        wt.resolve().relative_to(_worktrees_root().resolve())
    except (ValueError, OSError):
        _log.warning("remove_worktree: refusing unsafe path %s", wt)
        return
    repo = project_repo_path(project_id)
    if repo:
        _git(["worktree", "remove", "--force", str(wt)], repo)
        _git(["branch", "-D", _branch(session_id)], repo)
        _git(["worktree", "prune"], repo)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)


@dataclass
class DesktopTenantContext:
    """Minimal tenant context for desktop: workspace() yields the bound project's
    root **in-place** (or a flat scratch dir when no project is bound)."""

    tenant_id: str
    user_id: str
    repo_path: Optional[str]

    def workspace(self, session_id: str) -> WorkspaceRoot:
        if self.repo_path and os.path.isdir(self.repo_path):
            # In-place: the agent works directly in the project root (a git repo),
            # so edits land in the user's real files. Per-session git-worktree
            # isolation is intentionally OFF for the single-user desktop app —
            # ensure_worktree/session_worktree_path stay defined but dormant,
            # reserved for a future "isolate this session" toggle or if
            # concurrent/background workers are added.
            abs_path = self.repo_path
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
