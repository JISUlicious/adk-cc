"""Sandbox layer — the multi-tenancy gate.

Tools that touch the host (BashTool, WriteFileTool, EditFileTool,
ReadFileTool) call into a `SandboxBackend` instead of the OS directly.
The backend is selected by env var (`ADK_CC_SANDBOX_BACKEND`) and
attached to session state by the runner; tools resolve it via
`get_backend(tool_context)`.
"""

from __future__ import annotations

import os

from google.adk.tools.tool_context import ToolContext

from .backends import DockerBackend, E2BBackend, NoopBackend, SandboxBackend
from .config import (
    ExecResult,
    FsReadConfig,
    FsWriteConfig,
    NetworkConfig,
    SandboxViolation,
)
from .workspace import WorkspaceRoot, default_workspace, get_workspace, set_workspace

_STATE_KEY = "sandbox_backend"


def make_default_backend() -> SandboxBackend:
    """Construct the backend named by `ADK_CC_SANDBOX_BACKEND` (default: noop).

    Stub backends (docker, e2b) raise NotImplementedError on use; selecting
    them without an operator implementation is a deployment misconfig that
    will surface on the first tool call.
    """
    name = os.environ.get("ADK_CC_SANDBOX_BACKEND", "noop").lower()
    if name == "noop":
        return NoopBackend()
    if name == "docker":
        return DockerBackend()
    if name == "e2b":
        return E2BBackend()
    raise ValueError(f"unknown sandbox backend: {name!r}")


_default_backend: SandboxBackend | None = None


def _get_default_backend() -> SandboxBackend:
    global _default_backend
    if _default_backend is None:
        _default_backend = make_default_backend()
    return _default_backend


def get_backend(ctx: ToolContext) -> SandboxBackend:
    """Resolve the active backend, with a module-level fallback.

    Stage G's tenancy plugin will seed `ctx.state[_STATE_KEY]` per session;
    until then, the module-level singleton handles the dev path.
    """
    try:
        b = ctx.state.get(_STATE_KEY)
    except Exception:
        b = None
    if isinstance(b, SandboxBackend):
        return b
    return _get_default_backend()


def set_backend(ctx: ToolContext, backend: SandboxBackend) -> None:
    ctx.state[_STATE_KEY] = backend


__all__ = [
    "SandboxBackend",
    "NoopBackend",
    "DockerBackend",
    "E2BBackend",
    "ExecResult",
    "FsReadConfig",
    "FsWriteConfig",
    "NetworkConfig",
    "SandboxViolation",
    "WorkspaceRoot",
    "default_workspace",
    "get_workspace",
    "set_workspace",
    "make_default_backend",
    "get_backend",
    "set_backend",
]
