"""Tenant context + plugin that seeds it into session state.

For multi-tenant deployments, every tool call must know which tenant /
session / workspace it belongs to. The TenancyPlugin runs first in the
plugin chain on `before_run_callback` and writes the tenant context into
the session's state, where downstream tools (sandbox-backed FS, tasks,
etc.) read it.

How a request becomes a TenantContext: that's auth middleware's job
(`service/auth.py`). The middleware extracts a JWT or API key, looks up
the tenant, and attaches a TenantContext to the FastAPI request. ADK's
session-creation path then carries that context into the session state,
where this plugin reads it back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.tool_context import ToolContext

from ..sandbox import SandboxBackend, WorkspaceRoot, make_default_backend


def _safe_id(value: str, label: str) -> str:
    """Reject path-traversal in ids before they hit os.path.join.

    Mirrors the pattern in `EncryptedFileCredentialProvider._safe_component`.
    Tenant / user / session ids come from auth-validated request state; we
    still defense-in-depth here because a single mistyped resolver could
    otherwise let a `..`-laden id escape the workspace root.
    """
    safe = "".join(c for c in value if c.isalnum() or c in "-_")
    if safe != value or not safe:
        raise ValueError(f"unsafe {label} for filesystem path: {value!r}")
    return safe


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    user_id: str
    workspace_root_path: str  # absolute filesystem path — the ROOT under
                              # which per-tenant / per-user dirs live

    def workspace(self, session_id: str) -> WorkspaceRoot:
        """Build the per-user / per-session WorkspaceRoot.

        Layout:
            <workspace_root_path>/<tenant>/<user>/                ← home (persistent)
            <workspace_root_path>/<tenant>/<user>/.sessions/<sid>/ ← scratch (ephemeral)

        Both dirs are created on first access. A janitor (see
        `scripts/scratch_reaper.py`) reaps old scratch dirs.
        """
        safe_tenant = _safe_id(self.tenant_id, "tenant_id")
        safe_user = _safe_id(self.user_id, "user_id")
        safe_session = _safe_id(session_id, "session_id")

        user_home = os.path.join(
            self.workspace_root_path, safe_tenant, safe_user
        )
        scratch = os.path.join(user_home, ".sessions", safe_session)

        os.makedirs(user_home, exist_ok=True)
        os.makedirs(scratch, exist_ok=True)

        return WorkspaceRoot(
            tenant_id=self.tenant_id,
            session_id=session_id,
            abs_path=user_home,
            session_scratch_path=scratch,
        )


# `temp:` prefix — ADK's session service skips temp-keyed state in
# state-delta extraction. TenantContext is a dataclass, not JSON-
# serializable; persisting risks json.dumps failures and stale-session
# timestamp skew during HITL flows (e.g. tool-confirmation pause/resume).
_STATE_TENANT_KEY = "temp:tenant_context"


class TenancyPlugin(BasePlugin):
    """Seeds session state with the tenant's workspace + sandbox backend.

    Operators pass either:
      - `default_workspace_root` for a single-tenant deployment, or
      - a callable `tenant_resolver(user_id) -> TenantContext` that the
        plugin invokes once per session.

    For multi-tenant production, the resolver typically reads from a DB
    keyed on the JWT subject. For development, the default resolver maps
    every user_id to the same workspace.
    """

    def __init__(
        self,
        *,
        default_workspace_root: Optional[str] = None,
        tenant_resolver: Optional[
            "callable[[str], TenantContext]"  # noqa: F821 — string for forward ref
        ] = None,
        backend_factory: Optional[
            "callable[[TenantContext, str], SandboxBackend]"  # noqa: F821
        ] = None,
        name: str = "adk_cc_tenancy",
    ) -> None:
        super().__init__(name=name)
        self._default_root = (
            default_workspace_root
            or os.environ.get("ADK_CC_WORKSPACE_ROOT")
            or os.getcwd()
        )
        self._tenant_resolver = tenant_resolver or self._default_resolver
        self._backend_factory = backend_factory or (
            lambda ctx, session_id: make_default_backend(
                session_id=session_id, tenant_id=ctx.tenant_id
            )
        )

    def _default_resolver(self, user_id: str) -> TenantContext:
        """Default tenant resolution.

        Order of precedence:
          1. If the auth middleware set an auth context for this
             request (JWT or BearerToken extractor), use the
             tenant_id from there. This bridges the JWT's tenant
             claim into the plugin layer without requiring operators
             to supply a custom resolver.
          2. Otherwise (dev `adk web .`, unit tests, no-auth runs),
             fall back to the legacy "local" tenant. Single-tenant
             behavior is preserved when there's no auth context.

        Operators who need a different mapping (e.g. user_id → tenant
        via a database lookup, or a tenant-per-feature-flag scheme)
        still supply a custom `tenant_resolver` that overrides this
        method entirely.
        """
        # Lazy import to keep `service/auth.py` an optional dep — it
        # pulls in fastapi/starlette which aren't required for the
        # `adk web .` dev path.
        try:
            from .auth import get_auth_context
            auth = get_auth_context()
        except Exception:  # noqa: BLE001 — never block tenant resolution
            auth = None

        if auth is not None:
            auth_user_id, auth_tenant_id = auth
            return TenantContext(
                tenant_id=auth_tenant_id,
                # Trust the explicit user_id passed in; fall back to
                # the auth-provided one. Both should match in practice
                # (ADK's session uses auth-provided user_id) but
                # passing user_id is the public API of the resolver.
                user_id=user_id or auth_user_id or "local",
                workspace_root_path=self._default_root,
            )
        return TenantContext(
            tenant_id="local",
            user_id=user_id or "local",
            workspace_root_path=self._default_root,
        )

    async def before_tool_callback(
        self,
        *,
        tool,  # noqa: ANN001 — typed by ADK
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        # Lazy seeding: cheaper than wiring every session-creation path,
        # and idempotent — we re-check the keys per call but only write
        # once per session.
        try:
            state = tool_context.state
            if state.get(_STATE_TENANT_KEY) is None:
                tenant = self._tenant_resolver(getattr(tool_context, "user_id", ""))
                state[_STATE_TENANT_KEY] = tenant

                # Seed sandbox workspace + backend so the tool layer's
                # get_workspace / get_backend find them.
                from ..sandbox import set_backend, set_workspace

                session = getattr(tool_context, "session", None)
                session_id = getattr(session, "id", None) or "local"
                ws = tenant.workspace(session_id)
                backend = self._backend_factory(tenant, session_id)
                set_workspace(tool_context, ws)
                set_backend(tool_context, backend)

                # Best-effort workspace creation. Backends with no remote
                # surface (Noop) mkdir locally; DockerBackend creates the
                # dir on the sandbox VM via a one-shot helper container.
                try:
                    await backend.ensure_workspace(ws)
                except Exception:
                    pass
        except Exception:
            # Never crash the tool chain; missing tenancy degrades to
            # the default workspace + backend.
            pass
        return None

    async def after_run_callback(
        self,
        *,
        invocation_context,  # noqa: ANN001 — typed by ADK
    ) -> None:
        """Tear down per-session backend state when the run ends.

        Best-effort; failures are swallowed so a stuck cleanup doesn't
        block the next session.
        """
        try:
            session = getattr(invocation_context, "session", None)
            state = getattr(session, "state", None) or {}
            backend = state.get("temp:sandbox_backend")
            if backend is not None:
                await backend.close()
        except Exception:
            pass
