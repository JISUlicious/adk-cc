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


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    user_id: str
    workspace_root_path: str  # absolute filesystem path

    def workspace(self, session_id: str) -> WorkspaceRoot:
        return WorkspaceRoot(
            tenant_id=self.tenant_id,
            session_id=session_id,
            abs_path=self.workspace_root_path,
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
        backend_factory: Optional["callable[[TenantContext], SandboxBackend]"] = None,  # noqa: F821
        name: str = "adk_cc_tenancy",
    ) -> None:
        super().__init__(name=name)
        self._default_root = (
            default_workspace_root
            or os.environ.get("ADK_CC_WORKSPACE_ROOT")
            or os.getcwd()
        )
        self._tenant_resolver = tenant_resolver or self._default_resolver
        self._backend_factory = backend_factory or (lambda _ctx: make_default_backend())

    def _default_resolver(self, user_id: str) -> TenantContext:
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
                backend = self._backend_factory(tenant)
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
