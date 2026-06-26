"""Phase 3: TenantResourceRegistry gains a user dimension + list_union.

User-over-tenant, mirroring CredentialProvider: writes/reads are scope-exact;
list_union returns tenant ∪ user with the user winning on id collision; paths
are traversal-safe. Model-free.

Run: .venv/bin/python tests/test_registry_user_scope.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from pydantic import BaseModel  # noqa: E402

from adk_cc.service.registry import JsonFileTenantResourceRegistry  # noqa: E402


class Server(BaseModel):
    server_name: str
    url: str = ""


def _reg():
    d = tempfile.mkdtemp(prefix="reg-user-")
    return JsonFileTenantResourceRegistry(
        root=d, kind="mcp", model=Server, id_attr="server_name"
    )


def _names(rows):
    return {r.server_name for r in rows}


async def _run():
    reg = _reg()
    # tenant-shared server, alice's personal server, and a collision (alice's
    # "github" with a different url shadows the tenant "github")
    await reg.add(tenant_id="acme", resource=Server(server_name="github", url="tenant"))
    await reg.add(tenant_id="acme", resource=Server(server_name="personal", url="u"), user_id="alice")
    await reg.add(tenant_id="acme", resource=Server(server_name="github", url="alice"), user_id="alice")

    # scope-exact reads
    assert _names(await reg.list_for_tenant("acme")) == {"github"}
    assert _names(await reg.list_for_tenant("acme", "alice")) == {"github", "personal"}
    assert await reg.list_for_tenant("acme", "bob") == []

    # union: alice sees both, her github wins
    u = await reg.list_union("acme", "alice")
    assert _names(u) == {"github", "personal"}
    assert next(r for r in u if r.server_name == "github").url == "alice"  # user wins
    # bob: only tenant
    assert _names(await reg.list_union("acme", "bob")) == {"github"}
    # no user_id → tenant scope only
    assert _names(await reg.list_union("acme")) == {"github"}

    # scope-exact remove: drop alice's github → union falls back to tenant's
    await reg.remove(tenant_id="acme", resource_id="github", user_id="alice")
    u2 = await reg.list_union("acme", "alice")
    assert next(r for r in u2 if r.server_name == "github").url == "tenant"
    assert _names(await reg.list_for_tenant("acme")) == {"github"}  # tenant intact

    # cross-tenant isolation
    assert await reg.list_union("beta", "alice") == []


def test_registry_user_dimension():
    asyncio.run(_run())


def test_registry_traversal_safe():
    reg = _reg()

    async def _go():
        for bad in ["../escape", "a/b", ".."]:
            try:
                await reg.add(tenant_id="acme", resource=Server(server_name="x"), user_id=bad)
                raise AssertionError(f"unsafe user_id accepted: {bad!r}")
            except ValueError:
                pass

    asyncio.run(_go())


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
    print("\nall registry-user-scope tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
