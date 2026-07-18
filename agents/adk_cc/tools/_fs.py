"""Workspace-aware path resolution.

In Stage A this was a simple `Path.expanduser().resolve()`. In Stage C
it resolves a tool-supplied path against the active session's workspace
root: relative paths anchor under the workspace, absolute paths still
resolve absolutely (the sandbox backend's fs_read/fs_write configs
ultimately decide whether the resolved path is allowed).
"""

from __future__ import annotations

import os
from pathlib import Path

from google.adk.tools.tool_context import ToolContext

from ..sandbox import get_workspace


def display_path(p: str | Path, ctx: ToolContext | None = None) -> str:
    """Path string to show the model in tool results.

    Returns the path RELATIVE to the workspace root when it falls under
    it, otherwise the absolute path. Keeping host-absolute paths out of
    the model's view is what stops it from copying a server path like
    `/home/user/data/adk-cc/acme/alice/x.py` into a `run_bash` command —
    that command executes inside the sandbox, where the file lives at a
    different absolute path (e.g. `/home/daytona/x.py`). Relative paths
    are portable across the host↔sandbox boundary because every tool
    anchors them at the same workspace root / sandbox cwd.
    """
    if ctx is None:
        return str(p)
    try:
        ws = get_workspace(ctx)
        rel = os.path.relpath(str(p), ws.abs_path)
        # `..` means the path escapes the workspace — show it absolute.
        if not rel.startswith(".."):
            return rel
    except Exception:
        pass
    return str(p)


def resolve(path: str, ctx: ToolContext | None = None) -> Path:
    """Resolve `path` to an absolute path.

    - If `path` is absolute, expand and resolve.
    - If relative and a tool_context is given, anchor under the workspace.
    - If relative without a context, fall back to the process CWD.

    REMOTE workspaces (SshBackend): resolution is purely LEXICAL — no
    local `expanduser`/`realpath`, both of which would consult the WRONG
    machine's filesystem (macOS rewrites `/home/*` via its automount and
    `/tmp` → `/private/tmp`; `~` would expand to the local home). `..`
    still collapses lexically, and the backend's allow-path check decides
    whether the result stays in the workspace.
    """
    ws = None
    if ctx is not None:
        try:
            ws = get_workspace(ctx)
        except Exception:
            ws = None
    if ws is not None and getattr(ws, "remote", False):
        import posixpath

        raw = path if posixpath.isabs(path) else posixpath.join(ws.abs_path, path)
        return Path(posixpath.normpath(raw))
    p = Path(path).expanduser()
    if p.is_absolute():
        return p.resolve()
    if ws is not None:
        return (Path(ws.abs_path) / p).resolve()
    return p.resolve()
