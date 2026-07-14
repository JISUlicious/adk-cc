"""Sandbox layer — the multi-tenancy gate.

Tools that touch the host (BashTool, WriteFileTool, EditFileTool,
ReadFileTool) call into a `SandboxBackend` instead of the OS directly.
The backend is selected by env var (`ADK_CC_SANDBOX_BACKEND`) and
attached to session state by the runner; tools resolve it via
`get_backend(tool_context)`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_log = logging.getLogger(__name__)

from google.adk.tools.tool_context import ToolContext

from .. import deployment
from .backends import (
    DaytonaBackend,
    DockerBackend,
    E2BBackend,
    LocalContainerBackend,
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
from .workspace import (
    WorkspaceRoot,
    add_granted_root,
    clear_grant_once,
    default_workspace,
    discard_grant_once,
    get_workspace,
    grant_once,
    list_granted_roots,
    remove_granted_root,
    set_workspace,
)

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
    user_id: str = "local",
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

    Every backend is then wired for ON-DEMAND env injection
    (`configure_runtime_env`): the session user's secrets (user-over-tenant)
    plus the operator `SandboxEnvSpec` are resolved at exec time and merged
    into each command's environment. `user_id` selects the personal secret
    scope.
    """
    name = deployment.sandbox_backend_name()
    if name == "noop":
        backend: SandboxBackend = NoopBackend()
    elif name == "container":
        # Desktop-local Docker/Podman: shell isolated, project mounted in-place.
        from .backends.container_runtime import detect_runtime, reset_cache
        from .backends.local_container_backend import UnavailableSandboxBackend

        rt = detect_runtime()
        if rt is None:
            # Re-probe ONCE (uncached) — the process-wide cache may have pinned
            # None if the app booted before Docker Desktop finished starting.
            reset_cache()
            rt = detect_runtime()
        if rt is not None:
            backend = LocalContainerBackend(
                session_id=session_id,
                tenant_id=tenant_id,
                runtime=rt,
                image=deployment.sandbox_image(),
                network_enabled=deployment.sandbox_network_enabled(),
            )
        elif deployment.sandbox_require():
            # Fail CLOSED — the user demanded isolation; don't silently run on host.
            _log.warning("sandbox: container required but no runtime available — "
                         "run_bash will error (ADK_CC_SANDBOX_REQUIRE)")
            backend = UnavailableSandboxBackend()
        else:
            # Fail open to host exec, but LOUDLY — never silent (review #2).
            _log.warning("sandbox: 'container' requested but no Docker/Podman runtime "
                         "was found — falling back to HOST execution")
            backend = NoopBackend()
    elif name == "docker":
        backend = DockerBackend(session_id=session_id, tenant_id=tenant_id)
    elif name == "e2b":
        backend = E2BBackend()
    elif name == "sandbox_service":
        from .backends.sandbox_service_backend import (
            make_sandbox_service_backend_from_env,
        )

        backend = make_sandbox_service_backend_from_env(
            session_id=session_id,
            tenant_id=tenant_id,
            credentials=credentials,
        )
    elif name == "daytona":
        from .backends.daytona_backend import make_daytona_backend_from_env

        backend = make_daytona_backend_from_env(
            session_id=session_id,
            tenant_id=tenant_id,
            credentials=credentials,
        )
    else:
        raise ValueError(f"unknown sandbox backend: {name!r}")

    # Wire on-demand env injection (no-op if no provider/spec is configured).
    try:
        from .sandbox_env import sandbox_env_spec_from_env

        creds = credentials
        if creds is None:
            from ..credentials import credential_provider_from_env

            creds = credential_provider_from_env()
        # Least-privilege allowlist: the secrets the installed skills DECLARE
        # they need (metadata["x-adk-cc/secrets"]). Empty → inject all the
        # user's secrets (pre-declaration fallback). Cached across sessions.
        from ..credentials.required_inputs import declared_secret_keys

        backend.configure_runtime_env(
            credentials=creds,
            tenant_id=tenant_id,
            user_id=user_id,
            env_spec=sandbox_env_spec_from_env(),
            declared_keys=declared_secret_keys(tenant_id, user_id),
        )
    except Exception:  # noqa: BLE001 — env wiring must never break backend bring-up
        pass
    return backend


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


def is_noop_backend(backend: SandboxBackend) -> bool:
    """True if `backend` is the no-isolation host-exec NoopBackend.

    Used by the artifact tools to refuse at call time when the RESOLVED
    backend is noop (e.g. a per-session/tenant override the
    construction-time env check didn't see). Checks the backend's `name`
    so it works for any Noop-shaped backend without importing the class."""
    return getattr(backend, "name", None) == "noop"


__all__ = [
    "SandboxBackend",
    "NoopBackend",
    "DockerBackend",
    "LocalContainerBackend",
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
    "add_granted_root",
    "remove_granted_root",
    "list_granted_roots",
    "grant_once",
    "discard_grant_once",
    "clear_grant_once",
    "make_default_backend",
    "get_backend",
    "set_backend",
    "is_noop_backend",
]
