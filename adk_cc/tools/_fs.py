"""Workspace-aware path resolution.

In Stage A this was a simple `Path.expanduser().resolve()`. In Stage C
it resolves a tool-supplied path against the active session's workspace
root: relative paths anchor under the workspace, absolute paths still
resolve absolutely (the sandbox backend's fs_read/fs_write configs
ultimately decide whether the resolved path is allowed).
"""

from __future__ import annotations

from pathlib import Path

from google.adk.tools.tool_context import ToolContext

from ..sandbox import get_workspace


def resolve(path: str, ctx: ToolContext | None = None) -> Path:
    """Resolve `path` to an absolute path.

    - If `path` is absolute, expand and resolve.
    - If relative and a tool_context is given, anchor under the workspace.
    - If relative without a context, fall back to the process CWD.
    """
    p = Path(path).expanduser()
    if p.is_absolute():
        return p.resolve()
    if ctx is not None:
        ws = get_workspace(ctx)
        return (Path(ws.abs_path) / p).resolve()
    return p.resolve()
