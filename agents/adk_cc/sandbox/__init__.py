"""Sandbox layer — the multi-tenancy gate.

Tools that touch the host (BashTool, WriteFileTool, EditFileTool,
ReadFileTool) call into a `SandboxBackend` instead of the OS directly.
The backend is selected by env var (`ADK_CC_SANDBOX_BACKEND`) and
attached to session state by the runner; tools resolve it via
`get_backend(tool_context)`.
"""

from __future__ import annotations

import os
from typing import Any

from google.adk.tools.tool_context import ToolContext

from .backends import (
    DaytonaBackend,
    DockerBackend,
    E2BBackend,
    NoopBackend,
    SandboxBackend,
    SandboxServiceBackend,
)
from .config import (
    ExecResult,
    FsReadConfig,
    FsWriteConfig,
    NetworkConfig,
    SandboxViolation,
)
from .workspace import WorkspaceRoot, default_workspace, get_workspace, set_workspace

# `temp:` prefix tells ADK's session service to skip this key in state-delta
# extraction (`_session_util.extract_state_delta`). The backend object is a
# runtime handle (NoopBackend / DockerBackend instance), not JSON-serializable;
# putting it in persisted state risks `json.dumps` failures and stale-session
# version skew when ADK's storage timestamp diverges from the in-memory ref.
_STATE_KEY = "temp:sandbox_backend"


def make_default_backend(
    *,
    session_id: str = "local",
    tenant_id: str = "local",
    credentials: Any = None,  # Optional[CredentialProvider] — Any to keep the import lazy
) -> SandboxBackend:
    """Construct the backend named by `ADK_CC_SANDBOX_BACKEND` (default: noop).

    `session_id` and `tenant_id` are passed to backends that bind a remote
    resource per session (DockerBackend's container, SandboxServiceBackend's
    upstream session). NoopBackend / E2BBackend ignore them.

    `credentials` is passed to `SandboxServiceBackend` and `DaytonaBackend`
    for per-tenant token lookup (production multi-tenant). When None
    and the corresponding `_SHARED_TOKEN` / `_API_KEY` env var is set,
    the backend uses the static token (dev / single-tenant). Other
    backends ignore it.
    """
    name = os.environ.get("ADK_CC_SANDBOX_BACKEND", "noop").lower()
    if name == "noop":
        return NoopBackend()
    if name == "docker":
        return DockerBackend(session_id=session_id, tenant_id=tenant_id)
    if name == "e2b":
        return E2BBackend()
    if name == "sandbox_service":
        from .backends.sandbox_service_backend import (
            make_sandbox_service_backend_from_env,
        )

        return make_sandbox_service_backend_from_env(
            session_id=session_id,
            tenant_id=tenant_id,
            credentials=credentials,
        )
    if name == "daytona":
        from .backends.daytona_backend import make_daytona_backend_from_env

        return make_daytona_backend_from_env(
            session_id=session_id,
            tenant_id=tenant_id,
            credentials=credentials,
        )
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
    "SandboxServiceBackend",
    "DaytonaBackend",
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
