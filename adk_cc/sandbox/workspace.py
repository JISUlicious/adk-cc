"""Per-session workspace root.

Two shapes:
  - **Dev** (`adk web .`, `default_workspace()` fallback): single flat
    directory. `abs_path = <ADK_CC_WORKSPACE_ROOT>` (resolved against
    CWD if relative). `session_scratch_path = None`. Behavior unchanged
    from the pre-multi-tenant baseline. Dev is single-user by definition;
    isolation has no meaning.
  - **Production** (via `TenancyPlugin` → `TenantContext.workspace()`):
    per-user persistent home + per-session scratch.
    `abs_path = <root>/<tenant>/<user>/` is the user's home (persists
    across sessions). `session_scratch_path = <user_home>/.sessions/<session>/`
    is per-session scratch (auto-reaped). Tools default to the home;
    models can address the scratch dir explicitly for throwaway work.

Tools call `get_workspace(tool_context)` to resolve paths against the
right root for the current session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from google.adk.tools.tool_context import ToolContext

from .config import FsReadConfig, FsWriteConfig

# `temp:` prefix — ADK's session service skips temp-keyed state in
# state-delta extraction. The WorkspaceRoot dataclass isn't JSON-
# serializable; persisting it would break ADK's session storage.
_STATE_KEY = "temp:sandbox_workspace"


@dataclass(frozen=True)
class WorkspaceRoot:
    tenant_id: str
    session_id: str
    abs_path: str
    # Set by `TenantContext.workspace()` in production to enable
    # per-session scratch. None for the dev path (`default_workspace()`),
    # so dev fs configs and bind-mounts behave exactly as before.
    session_scratch_path: Optional[str] = None

    def __post_init__(self) -> None:
        # Canonicalize so the allow_paths match what Path.resolve() returns
        # for files inside the workspace. Without this, symlinked roots
        # (e.g. macOS /var → /private/var, /tmp → /private/tmp) cause every
        # in-workspace path check to fail.
        canonical = os.path.realpath(self.abs_path)
        if canonical != self.abs_path:
            object.__setattr__(self, "abs_path", canonical)
        if self.session_scratch_path:
            scratch_canonical = os.path.realpath(self.session_scratch_path)
            if scratch_canonical != self.session_scratch_path:
                object.__setattr__(self, "session_scratch_path", scratch_canonical)

    def _allow_paths(self) -> tuple[str, ...]:
        paths = (f"{self.abs_path}/**", self.abs_path)
        if self.session_scratch_path:
            paths = paths + (
                f"{self.session_scratch_path}/**",
                self.session_scratch_path,
            )
        return paths

    def fs_read_config(self) -> FsReadConfig:
        return FsReadConfig(allow_paths=self._allow_paths())

    def fs_write_config(self) -> FsWriteConfig:
        return FsWriteConfig(allow_paths=self._allow_paths())


def default_workspace() -> WorkspaceRoot:
    """Workspace used when none is seeded into session state.

    Read order:
      1. `ADK_CC_WORKSPACE_ROOT` env var — explicit configuration.
         Resolved against CWD if relative (e.g. `./.workspace`).
         Created on first use if it doesn't exist.
      2. CWD — last-resort fallback.

    Sufficient for `adk web .` on a developer laptop. In production
    multi-tenant deployments, `TenancyPlugin` seeds a per-tenant
    workspace into session state; this default isn't reached.
    """
    raw = os.environ.get("ADK_CC_WORKSPACE_ROOT")
    if raw:
        path = os.path.abspath(os.path.expanduser(raw))
        # Create on first use so the agent's first read/write doesn't
        # trip on a missing dir. NoopBackend's ensure_workspace would
        # mkdir later, but we want the path resolved before any tool
        # call so fs_write_config's allow_paths is correct.
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
    else:
        path = os.path.abspath(os.getcwd())
    return WorkspaceRoot(
        tenant_id="local",
        session_id="local",
        abs_path=path,
    )


def get_workspace(ctx: ToolContext) -> WorkspaceRoot:
    """Resolve the active workspace from session state, with a dev fallback."""
    try:
        raw = ctx.state.get(_STATE_KEY)
    except Exception:
        raw = None
    if isinstance(raw, WorkspaceRoot):
        return raw
    if isinstance(raw, dict):
        return WorkspaceRoot(**raw)
    return default_workspace()


def set_workspace(ctx: ToolContext, ws: WorkspaceRoot) -> None:
    ctx.state[_STATE_KEY] = ws
