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

# Desktop-only "granted directories" that widen the workspace scope beyond the
# bound project (see analysis/floating-roaming-spark plan). All three are lists
# of absolute path strings so they round-trip through the session DB serializer.
#   - session: granted for this session (persisted with the session record)
#   - user:   granted across the user's future sessions ("Working directories")
#   - once:   one-shot grant for the very next matching operation (temp-keyed,
#             cleared by the permission plugin's after_tool_callback)
# Folded into the effective allow_paths by `get_workspace`, DESKTOP ONLY — never
# in web/multi-tenant, where the hard per-tenant sandbox boundary must hold.
_SESSION_ROOTS_KEY = "adk_cc_extra_roots"
_USER_ROOTS_KEY = "user:adk_cc_extra_roots"
_GRANT_ONCE_KEY = "temp:adk_cc_fs_grant_once"


@dataclass(frozen=True)
class WorkspaceRoot:
    tenant_id: str
    session_id: str
    abs_path: str
    # Set by `TenantContext.workspace()` in production to enable
    # per-session scratch. None for the dev path (`default_workspace()`),
    # so dev fs configs and bind-mounts behave exactly as before.
    session_scratch_path: Optional[str] = None
    # Desktop-only extra roots the user has granted (via a scope-expansion
    # prompt, `/add-dir`, or the persistent "Working directories" setting).
    # Folded in by `get_workspace` in desktop mode; empty everywhere else, so
    # web/multi-tenant allow_paths are byte-for-byte unchanged.
    extra_roots: tuple[str, ...] = ()

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
        # Canonicalize + de-dup granted roots (drop any that equal the primary
        # root — already covered — and any blanks).
        if self.extra_roots:
            seen: list[str] = []
            for r in self.extra_roots:
                if not r:
                    continue
                cr = os.path.realpath(r)
                if cr != self.abs_path and cr not in seen:
                    seen.append(cr)
            object.__setattr__(self, "extra_roots", tuple(seen))

    def _allow_paths(self) -> tuple[str, ...]:
        paths = (f"{self.abs_path}/**", self.abs_path)
        if self.session_scratch_path:
            paths = paths + (
                f"{self.session_scratch_path}/**",
                self.session_scratch_path,
            )
        for root in self.extra_roots:
            paths = paths + (f"{root}/**", root)
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
    """Resolve the active workspace from session state, with a dev fallback.

    In DESKTOP mode only, fold in the user's granted directories (session +
    persistent `user:` + one-shot) so the file tools' allow_paths cover them.
    Web/multi-tenant never folds these — the hard per-tenant boundary holds."""
    try:
        raw = ctx.state.get(_STATE_KEY)
    except Exception:
        raw = None
    if isinstance(raw, WorkspaceRoot):
        ws = raw
    elif isinstance(raw, dict):
        ws = WorkspaceRoot(**raw)
    else:
        ws = default_workspace()

    from .. import deployment

    if not deployment.is_desktop():
        return ws
    granted = list_granted_roots(ctx)
    if not granted:
        return ws
    # Merge (dataclasses.replace re-runs __post_init__ → canon + de-dup).
    import dataclasses

    return dataclasses.replace(ws, extra_roots=tuple(ws.extra_roots) + tuple(granted))


def set_workspace(ctx: ToolContext, ws: WorkspaceRoot) -> None:
    ctx.state[_STATE_KEY] = ws


# --- Desktop granted-directory helpers ------------------------------------
# Thin read/modify/write wrappers over the three state keys. All tolerate a
# missing/garbage value (→ empty). Callers gate on deployment.is_desktop().


def _read_roots(ctx: ToolContext, key: str) -> list[str]:
    try:
        raw = ctx.state.get(key) or []
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, str) and r]


def list_granted_roots(ctx: ToolContext) -> list[str]:
    """All directories granted for this context: session ∪ persistent(user) ∪
    one-shot. De-duplicated, order-stable."""
    out: list[str] = []
    for key in (_SESSION_ROOTS_KEY, _USER_ROOTS_KEY, _GRANT_ONCE_KEY):
        for r in _read_roots(ctx, key):
            if r not in out:
                out.append(r)
    return out


def add_granted_root(ctx: ToolContext, path: str, *, persist: bool = False) -> None:
    """Grant a directory. `persist=False` → session scope (this session only);
    `persist=True` → `user:` scope (survives across the user's future sessions,
    the "Working directories" setting). No-op for blanks."""
    if not path:
        return
    key = _USER_ROOTS_KEY if persist else _SESSION_ROOTS_KEY
    existing = _read_roots(ctx, key)
    canon = os.path.realpath(path)
    if canon not in existing:
        existing.append(canon)
        ctx.state[key] = existing


def remove_granted_root(ctx: ToolContext, path: str, *, persist: bool = False) -> None:
    """Revoke a previously granted directory from the given scope."""
    key = _USER_ROOTS_KEY if persist else _SESSION_ROOTS_KEY
    canon = os.path.realpath(path) if path else path
    ctx.state[key] = [r for r in _read_roots(ctx, key) if r != canon]


def grant_once(ctx: ToolContext, path: str) -> None:
    """One-shot grant: allow the very next matching operation, then be cleared
    by the permission plugin's after_tool_callback. Stored as the exact target
    path (file), not a directory."""
    if not path:
        return
    existing = _read_roots(ctx, _GRANT_ONCE_KEY)
    canon = os.path.realpath(path)
    if canon not in existing:
        existing.append(canon)
        ctx.state[_GRANT_ONCE_KEY] = existing


def clear_grant_once(ctx: ToolContext) -> None:
    """Drop all one-shot grants (called after the granted operation runs)."""
    try:
        if ctx.state.get(_GRANT_ONCE_KEY):
            ctx.state[_GRANT_ONCE_KEY] = []
    except Exception:
        pass
