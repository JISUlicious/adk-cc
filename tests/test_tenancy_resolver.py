"""Unit tests for TenancyPlugin's tenant resolution, including the
auth-context bridge that prevents the silent-default-tenant gotcha.

Background: TenancyPlugin's resolver signature is `(user_id) -> TenantContext`.
The JWT auth extractor produces `(user_id, tenant_id)` per request.
Pre-fix, the default resolver hardcoded `tenant_id="local"` regardless,
silently dropping JWT-provided tenant claims. The fix: the middleware
sets an `_AUTH_CTX` ContextVar; the default resolver reads it via
`get_auth_context()` and uses the auth-provided tenant_id when present.

Run: `uv run python tests/test_tenancy_resolver.py`
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")


def test_default_resolver_no_auth_context() -> None:
    """When no auth middleware has set an auth context, the default
    resolver returns tenant_id='local' (legacy single-tenant behavior).
    Covers the `adk web .` dev path."""
    from adk_cc.service.tenancy import TenancyPlugin

    plugin = TenancyPlugin(default_workspace_root="/tmp/wks")
    ctx = plugin._default_resolver("alice")
    assert ctx.tenant_id == "local", ctx
    assert ctx.user_id == "alice", ctx
    assert ctx.workspace_root_path == "/tmp/wks", ctx
    print("OK default_resolver_no_auth_context")


def test_default_resolver_uses_auth_context_tenant_id() -> None:
    """When auth context is set (JWT or BearerToken extractor ran),
    the default resolver reads the tenant_id from there. This is the
    core fix — pre-fix, the JWT's tenant claim was silently dropped."""
    from adk_cc.service.auth import set_auth_context, get_auth_context, _AUTH_CTX
    from adk_cc.service.tenancy import TenancyPlugin

    plugin = TenancyPlugin(default_workspace_root="/tmp/wks")

    # Sanity: contextvar is unset by default
    assert get_auth_context() is None

    token = set_auth_context("alice", "acme")
    try:
        ctx = plugin._default_resolver("alice")
        assert ctx.tenant_id == "acme", ctx
        assert ctx.user_id == "alice", ctx
        # Workspace root still comes from the plugin config, not the
        # auth context — auth provides identity, not config.
        assert ctx.workspace_root_path == "/tmp/wks", ctx
        print("OK default_resolver_uses_auth_context_tenant_id")
    finally:
        _AUTH_CTX.reset(token)


def test_default_resolver_user_id_arg_wins_over_auth_user() -> None:
    """If the resolver is called with a user_id arg, it takes precedence
    over the auth context's user_id. This preserves the existing
    contract where the resolver's parameter is the source of truth for
    user_id; auth_context only fills in tenant_id."""
    from adk_cc.service.auth import set_auth_context, _AUTH_CTX
    from adk_cc.service.tenancy import TenancyPlugin

    plugin = TenancyPlugin(default_workspace_root="/tmp/wks")
    token = set_auth_context("auth_user", "acme")
    try:
        ctx = plugin._default_resolver("explicit_user")
        assert ctx.user_id == "explicit_user", ctx
        assert ctx.tenant_id == "acme", ctx
        print("OK default_resolver_user_id_arg_wins_over_auth_user")
    finally:
        _AUTH_CTX.reset(token)


def test_default_resolver_falls_back_when_user_id_empty() -> None:
    """Empty user_id arg + auth context present → falls back to
    auth's user_id. Empty user_id arg + no auth context → 'local'."""
    from adk_cc.service.auth import set_auth_context, _AUTH_CTX
    from adk_cc.service.tenancy import TenancyPlugin

    plugin = TenancyPlugin(default_workspace_root="/tmp/wks")

    # Empty user_id, no auth → "local"
    ctx = plugin._default_resolver("")
    assert ctx.user_id == "local", ctx
    assert ctx.tenant_id == "local", ctx

    # Empty user_id, auth set → uses auth's user_id
    token = set_auth_context("auth_user", "acme")
    try:
        ctx = plugin._default_resolver("")
        assert ctx.user_id == "auth_user", ctx
        assert ctx.tenant_id == "acme", ctx
        print("OK default_resolver_falls_back_when_user_id_empty")
    finally:
        _AUTH_CTX.reset(token)


def test_custom_resolver_overrides_default() -> None:
    """Operator-supplied custom resolver takes precedence over the
    default — auth context is bypassed entirely. Ensures we haven't
    broken the existing extension point."""
    from adk_cc.service.auth import set_auth_context, _AUTH_CTX
    from adk_cc.service.tenancy import TenancyPlugin, TenantContext

    def custom(user_id: str) -> TenantContext:
        return TenantContext(
            tenant_id="custom-tenant",
            user_id=user_id,
            workspace_root_path="/custom/path",
        )

    plugin = TenancyPlugin(
        default_workspace_root="/tmp/wks",
        tenant_resolver=custom,
    )

    # Even with auth context set, the custom resolver's tenant wins.
    token = set_auth_context("alice", "acme")
    try:
        ctx = plugin._tenant_resolver("alice")
        assert ctx.tenant_id == "custom-tenant", ctx
        assert ctx.workspace_root_path == "/custom/path", ctx
        print("OK custom_resolver_overrides_default")
    finally:
        _AUTH_CTX.reset(token)


async def test_auth_context_propagates_across_async_tasks() -> None:
    """ContextVars set in one async task must propagate to child
    tasks (asyncio.create_task spawns inherit context). This is the
    invariant that makes the bridge work — TenancyPlugin's
    before_tool_callback may run in a child task spawned by ADK's
    runner, and we need it to see the middleware's auth context."""
    from adk_cc.service.auth import set_auth_context, get_auth_context, _AUTH_CTX

    seen_in_child: list = []

    async def child() -> None:
        seen_in_child.append(get_auth_context())

    token = set_auth_context("alice", "acme")
    try:
        # Spawn a child task; it inherits the parent's context
        task = asyncio.create_task(child())
        await task
        assert seen_in_child == [("alice", "acme")], seen_in_child
        print("OK auth_context_propagates_across_async_tasks")
    finally:
        _AUTH_CTX.reset(token)


async def test_auth_context_isolates_between_concurrent_requests() -> None:
    """Two concurrent 'requests' must see their own auth contexts.
    ContextVars are scoped per task chain — running each in its own
    context (as Starlette does per request) gives this isolation
    naturally. Simulate it here with `copy_context().run`."""
    import contextvars
    from adk_cc.service.auth import _AUTH_CTX, get_auth_context

    seen: dict[str, tuple | None] = {}

    async def request_handler(label: str, user: str, tenant: str) -> None:
        # Each request runs in its own copied context — same pattern
        # Starlette's middleware uses per-request.
        token = _AUTH_CTX.set((user, tenant))
        try:
            await asyncio.sleep(0.01)  # let the other task run
            seen[label] = get_auth_context()
        finally:
            _AUTH_CTX.reset(token)

    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()

    async def run_in_ctx(ctx, coro_factory):
        return await asyncio.create_task(coro_factory(), context=ctx)

    await asyncio.gather(
        run_in_ctx(ctx_a, lambda: request_handler("a", "alice", "acme")),
        run_in_ctx(ctx_b, lambda: request_handler("b", "bob", "beta")),
    )
    assert seen["a"] == ("alice", "acme"), seen
    assert seen["b"] == ("bob", "beta"), seen
    print("OK auth_context_isolates_between_concurrent_requests")


async def test_ensure_workspace_retries_after_failure() -> None:
    """Regression: a failed ensure_workspace must NOT stick — the flag is set
    only AFTER success, so the next tool call retries. Pre-fix, the flag flipped
    True before the call, so one transient failure (e.g. the sandbox API briefly
    unreachable) bricked the whole session with 'used before ensure_workspace()'.
    """
    from types import SimpleNamespace
    from adk_cc.service import tenancy as T
    from adk_cc.service.tenancy import TenancyPlugin, TenantContext
    from adk_cc.sandbox.backends.base import SandboxBackend

    # MUST subclass SandboxBackend — get_backend() ignores a state-stored
    # backend that fails isinstance(SandboxBackend) and falls back to the
    # module default, which would mask the behavior under test.
    class _FlakyBackend(SandboxBackend):
        name = "flaky"
        def __init__(self):
            self.calls = 0
        async def ensure_workspace(self, ws):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("daytona briefly unreachable")
            # 2nd call succeeds
        # unused ABC methods
        async def exec(self, *a, **k): raise NotImplementedError
        async def read_text(self, *a, **k): raise NotImplementedError
        async def write_text(self, *a, **k): raise NotImplementedError

    backend = _FlakyBackend()
    plugin = TenancyPlugin(default_workspace_root="/tmp/wks")

    # Pre-seed state so _seed_state is a no-op (tenant key already set) and
    # this test isolates ONLY the ensure-workspace retry logic — independent
    # of the real workspace/backend machinery and any .env-configured backend.
    state: dict = {
        T._STATE_TENANT_KEY: TenantContext(
            tenant_id="local", user_id="alice", workspace_root_path="/tmp/wks"
        ),
        "temp:sandbox_workspace": SimpleNamespace(
            abs_path="/tmp/wks/local/alice", tenant_id="local", session_id="s1",
            fs_read_config=lambda: None, fs_write_config=lambda: None,
        ),
        "temp:sandbox_backend": backend,
    }
    ctx = SimpleNamespace(
        state=state,
        user_id="alice",
        session=SimpleNamespace(id="s1", state=state),
    )

    # 1st tool call: ensure fails → flag must remain UNSET so we retry.
    await plugin.before_tool_callback(tool=None, tool_args={}, tool_context=ctx)
    assert backend.calls == 1, backend.calls
    assert not state.get(T._WS_ENSURED_KEY), "flag must NOT be set after a failed ensure"

    # 2nd tool call: ensure retried and succeeds → flag set, no further calls.
    await plugin.before_tool_callback(tool=None, tool_args={}, tool_context=ctx)
    assert backend.calls == 2, "ensure_workspace should have been retried"
    assert state.get(T._WS_ENSURED_KEY) is True, "flag must be set after success"

    # 3rd tool call: already ensured → no more ensure_workspace calls.
    await plugin.before_tool_callback(tool=None, tool_args={}, tool_context=ctx)
    assert backend.calls == 2, "ensure_workspace must not run again once ensured"
    print("OK ensure_workspace_retries_after_failure")


def main() -> None:
    test_default_resolver_no_auth_context()
    test_default_resolver_uses_auth_context_tenant_id()
    test_default_resolver_user_id_arg_wins_over_auth_user()
    test_default_resolver_falls_back_when_user_id_empty()
    test_custom_resolver_overrides_default()
    asyncio.run(test_auth_context_propagates_across_async_tasks())
    asyncio.run(test_auth_context_isolates_between_concurrent_requests())
    asyncio.run(test_ensure_workspace_retries_after_failure())
    print("\nall tenancy-resolver tests passed")


if __name__ == "__main__":
    main()
