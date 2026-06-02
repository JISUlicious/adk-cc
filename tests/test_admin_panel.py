"""Tests for the admin-panel backend foundation (Phase 1).

Covers:
  - the admin-role authorization hook (_make_admin_role_extractor): admin
    role required, configurable role name, global-tenant restriction;
  - MCP server CRUD through the mounted routes;
  - skill list/delete;
  - credential list (KEY NAMES only, values never returned) + put/delete;
  - the CredentialProvider.list_keys additions (in-memory shared singleton +
    isolated).

Builds a FastAPI app + auth middleware + mount_tenant_admin with the admin
role extractor, driven by TestClient (no uvicorn). Hand-rolled (no pytest).
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import zipfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from starlette.requests import Request  # noqa: E402,F401 — get_type_hints

from adk_cc.credentials import InMemoryCredentialProvider
from adk_cc.service.auth import AuthPrincipal, BearerTokenExtractor, make_auth_middleware
from adk_cc.service.admin_routes import mount_tenant_admin
from adk_cc.service.registry import JsonFileTenantResourceRegistry
from adk_cc.service.server import _make_admin_role_extractor
from adk_cc.tools.mcp_tenant import McpServerConfig


def _client(tmp, *, role_env=None):
    """Build a TestClient over an app with auth + admin routes (global tenant
    'local'), using the production admin-role extractor."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    # alice has the admin role; bob does not; carol has a custom role.
    tokmap = {
        "admintok": AuthPrincipal("alice", "local", frozenset({"admin"})),
        "usertok": AuthPrincipal("bob", "local", frozenset()),
        "customtok": AuthPrincipal("carol", "local", frozenset({"platform-admin"})),
        "othertenant": AuthPrincipal("dave", "other", frozenset({"admin"})),
    }
    registry = JsonFileTenantResourceRegistry[McpServerConfig](
        root=os.path.join(tmp, "registry"), kind="mcp",
        model=McpServerConfig, id_attr="server_name",
    )
    creds = InMemoryCredentialProvider(shared=False)

    # Configure the extractor via env (global tenant 'local', role name).
    prev = {}
    for k, v in {"ADK_CC_GLOBAL_TENANT_ID": "local",
                 "ADK_CC_ADMIN_ROLE": role_env or "admin"}.items():
        prev[k] = os.environ.get(k)
        os.environ[k] = v
    extractor = _make_admin_role_extractor()
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    app = FastAPI()
    mount_tenant_admin(
        app, registry=registry, credentials=creds,
        skill_root=os.path.join(tmp, "skills"),
        admin_extractor=extractor,
    )
    app.add_middleware(make_auth_middleware(BearerTokenExtractor(tokmap)))
    return TestClient(app), creds


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


# --- admin role gate ------------------------------------------------------

def test_non_admin_forbidden():
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        r = c.get("/tenants/local/mcp-servers", headers=_h("usertok"))
        assert r.status_code == 403, r.text
    print("OK test_non_admin_forbidden")


def test_admin_permitted():
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        r = c.get("/tenants/local/mcp-servers", headers=_h("admintok"))
        assert r.status_code == 200, r.text
        assert r.json() == {"servers": []}
    print("OK test_admin_permitted")


def test_unauthenticated_401():
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        r = c.get("/tenants/local/mcp-servers")
        assert r.status_code == 401, r.text
    print("OK test_unauthenticated_401")


def test_admin_wrong_tenant_forbidden():
    # An admin acting on a tenant other than the global one is rejected.
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        r = c.get("/tenants/other/mcp-servers", headers=_h("othertenant"))
        assert r.status_code == 403, r.text
    print("OK test_admin_wrong_tenant_forbidden")


def test_configurable_role_name():
    # With ADK_CC_ADMIN_ROLE=platform-admin, carol (who holds it) is allowed
    # and alice (plain 'admin') is not.
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp, role_env="platform-admin")
        assert c.get("/tenants/local/mcp-servers", headers=_h("customtok")).status_code == 200
        assert c.get("/tenants/local/mcp-servers", headers=_h("admintok")).status_code == 403
    print("OK test_configurable_role_name")


# --- MCP CRUD -------------------------------------------------------------

def test_mcp_crud_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        # create
        r = c.put("/tenants/local/mcp-servers/github", headers=_h("admintok"),
                  json={"transport": "http", "url": "https://api.github.com/mcp"})
        assert r.status_code == 200, r.text
        # list
        servers = c.get("/tenants/local/mcp-servers", headers=_h("admintok")).json()["servers"]
        assert len(servers) == 1 and servers[0]["server_name"] == "github", servers
        # delete
        assert c.delete("/tenants/local/mcp-servers/github", headers=_h("admintok")).status_code == 200
        assert c.get("/tenants/local/mcp-servers", headers=_h("admintok")).json()["servers"] == []
    print("OK test_mcp_crud_roundtrip")


def test_mcp_invalid_config_400():
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        # missing required url/transport
        r = c.put("/tenants/local/mcp-servers/bad", headers=_h("admintok"), json={})
        assert r.status_code == 400, r.text
    print("OK test_mcp_invalid_config_400")


# --- credentials (names only) ---------------------------------------------

def test_credentials_list_names_not_values():
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        c.put("/tenants/local/credentials/github_token", headers=_h("admintok"),
              json={"value": "ghp_supersecret"})
        c.put("/tenants/local/credentials/slack_token", headers=_h("admintok"),
              json={"value": "xoxb-secret"})
        body = c.get("/tenants/local/credentials", headers=_h("admintok")).json()
        assert sorted(body["keys"]) == ["github_token", "slack_token"], body
        # the secret value must NOT appear anywhere in the response
        assert "ghp_supersecret" not in c.get(
            "/tenants/local/credentials", headers=_h("admintok")
        ).text
        # delete one
        c.delete("/tenants/local/credentials/github_token", headers=_h("admintok"))
        assert c.get("/tenants/local/credentials", headers=_h("admintok")).json()["keys"] == ["slack_token"]
    print("OK test_credentials_list_names_not_values")


def test_credentials_put_requires_value():
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        r = c.put("/tenants/local/credentials/k", headers=_h("admintok"), json={})
        assert r.status_code == 400, r.text
    print("OK test_credentials_put_requires_value")


# --- skills ---------------------------------------------------------------

def _skill_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "---\nname: demo\ndescription: d\n---\nbody")
    return buf.getvalue()


def test_skill_upload_list_delete():
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        assert c.get("/tenants/local/skills", headers=_h("admintok")).json() == {"skills": []}
        r = c.put("/tenants/local/skills/demo", headers=_h("admintok"),
                  content=_skill_zip())
        assert r.status_code == 200, r.text
        assert c.get("/tenants/local/skills", headers=_h("admintok")).json()["skills"] == ["demo"]
        assert c.delete("/tenants/local/skills/demo", headers=_h("admintok")).status_code == 200
        assert c.get("/tenants/local/skills", headers=_h("admintok")).json()["skills"] == []
    print("OK test_skill_upload_list_delete")


def test_skill_upload_rejects_non_zip():
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        r = c.put("/tenants/local/skills/x", headers=_h("admintok"), content=b"not a zip")
        assert r.status_code == 400, r.text
    print("OK test_skill_upload_rejects_non_zip")


# --- CredentialProvider.list_keys -----------------------------------------

def test_inmemory_list_keys_isolated():
    async def main():
        p = InMemoryCredentialProvider(shared=False)
        await p.put(tenant_id="t1", key="a", value="1")
        await p.put(tenant_id="t1", key="b", value="2")
        await p.put(tenant_id="t2", key="c", value="3")
        assert await p.list_keys(tenant_id="t1") == ["a", "b"]
        assert await p.list_keys(tenant_id="t2") == ["c"]
        assert await p.list_keys(tenant_id="none") == []
    asyncio.run(main())
    print("OK test_inmemory_list_keys_isolated")


if __name__ == "__main__":
    test_non_admin_forbidden()
    test_admin_permitted()
    test_unauthenticated_401()
    test_admin_wrong_tenant_forbidden()
    test_configurable_role_name()
    test_mcp_crud_roundtrip()
    test_mcp_invalid_config_400()
    test_credentials_list_names_not_values()
    test_credentials_put_requires_value()
    test_skill_upload_list_delete()
    test_skill_upload_rejects_non_zip()
    test_inmemory_list_keys_isolated()
    print("\nall admin-panel tests passed")
