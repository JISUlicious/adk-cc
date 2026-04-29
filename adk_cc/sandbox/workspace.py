"""Per-session workspace root.

In single-tenant dev this is the process CWD. In Stage G's web service
deployment, it's `/var/lib/adk-cc/wks/{tenant_id}/{session_id}` (or
whatever the operator chooses) and the tenancy plugin seeds it on
session creation.

Tools call `get_workspace(tool_context)` to resolve paths against the
right root for the current session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from google.adk.tools.tool_context import ToolContext

from .config import FsReadConfig, FsWriteConfig

_STATE_KEY = "sandbox_workspace"


@dataclass(frozen=True)
class WorkspaceRoot:
    tenant_id: str
    session_id: str
    abs_path: str

    def __post_init__(self) -> None:
        # Canonicalize so the allow_paths match what Path.resolve() returns
        # for files inside the workspace. Without this, symlinked roots
        # (e.g. macOS /var → /private/var, /tmp → /private/tmp) cause every
        # in-workspace path check to fail.
        canonical = os.path.realpath(self.abs_path)
        if canonical != self.abs_path:
            object.__setattr__(self, "abs_path", canonical)

    def fs_read_config(self) -> FsReadConfig:
        return FsReadConfig(allow_paths=(f"{self.abs_path}/**", self.abs_path))

    def fs_write_config(self) -> FsWriteConfig:
        return FsWriteConfig(allow_paths=(f"{self.abs_path}/**", self.abs_path))


def default_workspace() -> WorkspaceRoot:
    """Workspace used when none is seeded into session state.

    Sufficient for `adk web .` on a developer laptop. In production
    every tool call must come with a workspace seeded by the tenancy
    plugin, and this default is a safety net rather than a deployment
    target.
    """
    return WorkspaceRoot(
        tenant_id="local",
        session_id="local",
        abs_path=os.path.abspath(os.getcwd()),
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
