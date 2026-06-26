"""TenantMcpToolset resolves MCP tokens user-over-tenant (Phase 2, MCP half).

Verifies get_tools() passes the session user's id into CredentialProvider.get,
so a user's personal MCP token overrides the org's shared one. We don't connect
to a real MCP server — the resolver fetches the credential BEFORE building the
inner McpToolset, so a recording fake provider captures the call even though the
(bogus) connection then fails and the server is skipped.

Model-free. Run: .venv/bin/python tests/test_mcp_user_scope.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.tools.mcp_tenant import TenantMcpToolset  # noqa: E402


class _RecordingCreds:
    def __init__(self):
        self.calls = []

    async def get(self, *, tenant_id, key, user_id=None):
        self.calls.append((tenant_id, key, user_id))
        return "tok"

    async def put(self, *, tenant_id, key, value, user_id=None):
        pass

    async def delete(self, *, tenant_id, key, user_id=None):
        pass


class _FakeRegistry:
    def __init__(self, cfgs):
        self._cfgs = cfgs

    async def list_for_tenant(self, tenant_id, user_id=None):
        return self._cfgs

    async def list_union(self, tenant_id, user_id=None):
        return self._cfgs


def _ctx(tenant_id="acme", user_id="alice"):
    return SimpleNamespace(
        session=SimpleNamespace(
            state={"temp:tenant_context": SimpleNamespace(tenant_id=tenant_id, user_id=user_id)}
        )
    )


def _cfg():
    return SimpleNamespace(
        server_name="srv",
        transport="streamable_http",
        url="http://example.invalid/mcp",
        credential_key="MCP_TOKEN",
        tool_filter=None,
        require_confirmation=False,
        use_mcp_resources=False,
        save_resources_as_artifacts=False,
    )


def test_mcp_credential_resolved_with_user_id():
    creds = _RecordingCreds()
    ts = TenantMcpToolset(registry=_FakeRegistry([_cfg()]), credentials=creds)
    # connection will fail (example.invalid) and the server is skipped, but the
    # credential lookup happens first and is recorded.
    asyncio.run(ts.get_tools(_ctx(user_id="alice")))
    assert creds.calls == [("acme", "MCP_TOKEN", "alice")], creds.calls


def test_mcp_no_user_id_falls_back_to_tenant():
    creds = _RecordingCreds()
    ts = TenantMcpToolset(registry=_FakeRegistry([_cfg()]), credentials=creds)
    asyncio.run(ts.get_tools(_ctx(user_id="")))
    # empty user_id → None passed → tenant-shared resolution
    assert creds.calls == [("acme", "MCP_TOKEN", None)], creds.calls


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__[5:]}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__[5:]}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__[5:]}: {type(e).__name__}: {e}")
    print("\nall mcp-user-scope tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
