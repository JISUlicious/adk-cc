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

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.tool_context import ToolContext

from ..sandbox import SandboxBackend, WorkspaceRoot, make_default_backend

_log = logging.getLogger(__name__)


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
# Separate guard for the (potentially expensive) ensure_workspace call.
# State seeding now happens at invocation start (before_run_callback), so
# `_STATE_TENANT_KEY is None` no longer reliably marks "first tool call" —
# we track the backend bring-up with its own temp flag instead, so it
# still runs exactly once per invocation, at the first tool call.
_WS_ENSURED_KEY = "temp:sandbox_workspace_ensured"


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

    def _seed_state(self, state, *, user_id: str, session) -> None:
        """Resolve the tenant and seed workspace + backend into state.

        Cheap and idempotent: resolves once per invocation (guarded by
        the temp tenant key) and does NOT call ensure_workspace — backend
        bring-up stays lazy at the first tool call. Called from both
        before_run_callback (so get_workspace resolves the real bucket at
        a turn's OPENING, before any tool runs — the task reminder needs
        this) and before_tool_callback (belt-and-suspenders).
        """
        if state.get(_STATE_TENANT_KEY) is not None:
            return
        tenant = self._tenant_resolver(user_id or "")
        state[_STATE_TENANT_KEY] = tenant

        # Seed sandbox workspace + backend so the tool layer's
        # get_workspace / get_backend — and the task reminder — find them.
        from types import SimpleNamespace

        from ..sandbox import set_backend, set_workspace

        session_id = getattr(session, "id", None) or "local"
        ws = tenant.workspace(session_id)
        backend = self._backend_factory(tenant, session_id)
        # set_workspace / set_backend only touch ctx.state; a thin shim
        # lets us reuse them from before_run (which hands us an
        # InvocationContext, not a ToolContext).
        shim = SimpleNamespace(state=state)
        set_workspace(shim, ws)
        set_backend(shim, backend)

    async def before_run_callback(
        self,
        *,
        invocation_context,  # noqa: ANN001 — typed by ADK
    ) -> None:
        """Seed tenant/workspace state at invocation start.

        Runs before the first model call — and before TaskReminderPlugin,
        since TenancyPlugin is registered first — so get_workspace()
        resolves the per-tenant bucket from the very first
        before_model_callback, not just after the first tool call. The
        fresh-turn task reminder (which fires at a turn's opening) depends
        on this; without it the reminder reads the default `local`
        workspace and never sees the user's tasks.
        """
        try:
            session = getattr(invocation_context, "session", None)
            state = getattr(session, "state", None)
            if state is None:
                return
            self._seed_state(
                state,
                user_id=getattr(session, "user_id", "") or "",
                session=session,
            )
        except Exception:
            # Never block the run; missing tenancy degrades to the
            # default workspace + backend at tool time.
            pass

    async def before_tool_callback(
        self,
        *,
        tool,  # noqa: ANN001 — typed by ADK
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        try:
            state = tool_context.state
            # Idempotent — before_run_callback usually seeded this first.
            self._seed_state(
                state,
                user_id=getattr(tool_context, "user_id", ""),
                session=getattr(tool_context, "session", None),
            )

            # Backend bring-up: once per invocation, at the first tool
            # call. Guarded by its OWN flag (not the tenant key, which is
            # now set earlier in before_run). Backends with no remote
            # surface (Noop) mkdir locally; DockerBackend creates the dir
            # on the sandbox VM; DaytonaBackend / SandboxServiceBackend
            # hit an external API. A failure here leaves later tool calls
            # hitting a "backend used before ensure_workspace()" guard
            # with no visible root cause — so we log before swallowing.
            if not state.get(_WS_ENSURED_KEY):
                state[_WS_ENSURED_KEY] = True
                from ..sandbox import get_backend, get_workspace

                ws = get_workspace(tool_context)
                backend = get_backend(tool_context)
                try:
                    await backend.ensure_workspace(ws)
                except Exception as e:
                    _log.warning(
                        "ensure_workspace failed (backend=%s tenant=%s session=%s): %s: %s",
                        type(backend).__name__,
                        getattr(ws, "tenant_id", "?"),
                        getattr(ws, "session_id", "?"),
                        type(e).__name__,
                        e,
                    )
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
