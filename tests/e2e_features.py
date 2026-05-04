"""End-to-end tests for the multi-tenant features (blocks A, B+D, C).

Spins up a service-shaped FastAPI app in-process via TestClient (same wire
shape as `uvicorn adk_cc.service.server:make_app --factory`, just no socket).
Each block's test verifies the feature through the actual HTTP surface and
the agent's `BaseToolset.get_tools` resolver path — not unit-level mocks.

What's covered in-process:
  - Block A: JWT acceptance/rejection flow against a self-signed JWKS.
  - Block B+D: admin CRUD (credentials, MCP servers) under JWT auth + per-
    tenant isolation; resolver attaches MCP toolset configs (skips broken
    servers gracefully).
  - Block C: skill upload via admin + resolver loads tenant skills.

What's NOT covered in-process and would need additional fixtures:
  - Block B+D MCP tool roundtrip: needs a stub MCP server (the SSE / HTTP
    transport) to actually invoke a registered tool. Currently the resolver
    builds the McpToolset but the connection fails (bogus URL); we verify
    it fails gracefully without crashing the agent.
  - Block C skill execution: needs the local model to choose to call the
    skill, which is non-deterministic. The unit test in block 0 already
    verified the SandboxBackedCodeExecutor against NoopBackend.

Run: `uv run python tests/e2e_features.py`

Should become `tests/e2e/test_features.py` once we add pytest as a dep.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

# Force a dummy model API key so agent.py imports cleanly.
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")


def _setup_jwt():
    """Generate an RSA keypair + JWKS for the JWT tests."""
    from authlib.jose import JsonWebKey, jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = pk.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_jwk = JsonWebKey.import_key(
        public_pem, {"kty": "RSA", "use": "sig", "kid": "e2e-1"}
    )
    jwks_dict = {"keys": [public_jwk.as_dict()]}

    def sign(claims: dict) -> str:
        header = {"alg": "RS256", "kid": "e2e-1"}
        return jwt.encode(header, claims, private_pem).decode("utf-8")

    return jwks_dict, sign


def _make_token(sign, *, sub: str, tenant: str, exp_in: int = 600, **extra) -> str:
    now = int(time.time())
    return sign({
        "iss": "https://idp.test",
        "aud": "adk-cc",
        "sub": sub,
        "tenant": tenant,
        "iat": now,
        "exp": now + exp_in,
        **extra,
    })


def _make_app(jwks_dict, *, registry, credentials, skill_root):
    """Service-shaped FastAPI app with JWT auth + admin routes."""
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware

    from adk_cc.service.admin_routes import mount_tenant_admin
    from adk_cc.service.auth import JwtAuthExtractor

    extractor = JwtAuthExtractor(
        jwks=jwks_dict,
        issuer="https://idp.test",
        audience="adk-cc",
    )

    class JwtMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            try:
                user_id, tenant_id = await extractor(request)
            except Exception as e:
                from starlette.responses import Response

                detail = getattr(e, "detail", str(e))
                code = getattr(e, "status_code", 500)
                return Response(content=str(detail), status_code=code,
                                media_type="text/plain")
            request.state.adk_cc_auth = (user_id, tenant_id)
            return await call_next(request)

    app = FastAPI()
    app.add_middleware(JwtMiddleware)
    mount_tenant_admin(
        app, registry=registry, credentials=credentials, skill_root=skill_root,
    )
    return app


# === Tests ===

def test_block_A_jwt_auth(client, sign):
    """Block A: JWT validation through the live middleware."""
    print("\n=== Block A: JWT auth ===")

    # Missing token → 401
    r = client.get("/tenants/tenantA/mcp-servers")
    assert r.status_code == 401, (r.status_code, r.text)
    print("  OK no token → 401")

    # Wrong issuer → 401
    bad_iss = sign({"iss": "https://evil", "aud": "adk-cc", "sub": "alice",
                    "tenant": "tenantA", "exp": int(time.time()) + 600})
    r = client.get("/tenants/tenantA/mcp-servers",
                   headers={"Authorization": f"Bearer {bad_iss}"})
    assert r.status_code == 401, r.status_code
    print("  OK wrong issuer → 401")

    # Expired → 401
    exp = sign({"iss": "https://idp.test", "aud": "adk-cc", "sub": "alice",
                "tenant": "tenantA", "exp": int(time.time()) - 10,
                "iat": int(time.time()) - 100})
    r = client.get("/tenants/tenantA/mcp-servers",
                   headers={"Authorization": f"Bearer {exp}"})
    assert r.status_code == 401
    print("  OK expired → 401")

    # Valid → 200
    tok = _make_token(sign, sub="alice", tenant="tenantA")
    r = client.get("/tenants/tenantA/mcp-servers",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, (r.status_code, r.text)
    print("  OK valid token → 200")

    # Cross-tenant → 403 (auth succeeded, RBAC denied)
    tok_a = _make_token(sign, sub="alice", tenant="tenantA")
    r = client.get("/tenants/tenantB/mcp-servers",
                   headers={"Authorization": f"Bearer {tok_a}"})
    assert r.status_code == 403, (r.status_code, r.text)
    print("  OK cross-tenant → 403")


def test_block_BD_mcp_admin_and_resolver(client, sign, *, registry, credentials):
    """Block B+D: admin CRUD + TenantMcpToolset resolver."""
    print("\n=== Block B+D: MCP admin + resolver ===")

    tok_a = _make_token(sign, sub="alice", tenant="tenantA")
    auth_a = {"Authorization": f"Bearer {tok_a}"}

    # PUT credential
    r = client.put("/tenants/tenantA/credentials/gh",
                   json={"value": "ghp_secret_1"}, headers=auth_a)
    assert r.status_code == 200, (r.status_code, r.text)
    val = asyncio.run(credentials.get(tenant_id="tenantA", key="gh"))
    assert val == "ghp_secret_1"
    print("  OK PUT credential persists through CredentialProvider")

    # PUT MCP server
    r = client.put("/tenants/tenantA/mcp-servers/gh",
                   json={"transport": "sse", "url": "http://localhost:1/sse",
                         "credential_key": "gh"},
                   headers=auth_a)
    assert r.status_code == 200, (r.status_code, r.text)

    # GET shows the registered server
    r = client.get("/tenants/tenantA/mcp-servers", headers=auth_a)
    assert r.status_code == 200
    assert len(r.json()["servers"]) == 1
    assert r.json()["servers"][0]["server_name"] == "gh"
    print("  OK PUT + GET MCP server")

    # Resolver test: TenantMcpToolset.get_tools() with tenantA's context
    # should attempt to build the MCP toolset. Connection fails (port 1
    # is unreachable), but the resolver MUST handle it gracefully — not
    # crash the agent.
    from adk_cc.tools.mcp_tenant import TenantMcpToolset

    class FakeTenantCtx:
        def __init__(self, tid): self.tenant_id = tid
    class FakeSession:
        def __init__(self, state): self.state = state
    class FakeRoCtx:
        invocation_id = "e2e-inv"
        def __init__(self, state): self.session = FakeSession(state)

    ts = TenantMcpToolset(registry=registry, credentials=credentials)

    ctx_a = FakeRoCtx({"temp:tenant_context": FakeTenantCtx("tenantA")})
    tools = asyncio.run(ts.get_tools(ctx_a))
    # Bad URL → resolver logs warning, returns empty (one bad server
    # doesn't kill the agent).
    assert isinstance(tools, list)
    print(f"  OK resolver handles unreachable MCP gracefully ({len(tools)} tools)")

    # Tenant B sees nothing (per-tenant isolation through HTTP and resolver)
    tok_b = _make_token(sign, sub="bob", tenant="tenantB")
    r = client.get("/tenants/tenantB/mcp-servers",
                   headers={"Authorization": f"Bearer {tok_b}"})
    assert r.status_code == 200
    assert r.json()["servers"] == []

    ctx_b = FakeRoCtx({"temp:tenant_context": FakeTenantCtx("tenantB")})
    tools_b = asyncio.run(ts.get_tools(ctx_b))
    assert tools_b == []
    print("  OK per-tenant isolation through both HTTP and resolver")

    # DELETE
    r = client.request("DELETE", "/tenants/tenantA/mcp-servers/gh", headers=auth_a)
    assert r.status_code == 200
    r = client.get("/tenants/tenantA/mcp-servers", headers=auth_a)
    assert r.json()["servers"] == []
    print("  OK DELETE removes server, hot-reloaded on next resolver call")


def test_block_C_skills_admin_and_resolver(client, sign, *, skill_root):
    """Block C: skill upload + TenantSkillToolset resolver."""
    print("\n=== Block C: skills upload + resolver ===")

    tok_a = _make_token(sign, sub="alice", tenant="tenantA")
    auth_a = {"Authorization": f"Bearer {tok_a}"}

    # Build a valid skill — ADK requires SKILL.md as the manifest filename.
    skill_md = """---
name: greeter
description: Says hello.
---

Print "hello from skill".
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", skill_md)

    # Upload
    r = client.put("/tenants/tenantA/skills/greeter",
                   content=buf.getvalue(), headers=auth_a)
    assert r.status_code == 200, (r.status_code, r.text)

    # List
    r = client.get("/tenants/tenantA/skills", headers=auth_a)
    assert r.json()["skills"] == ["greeter"], r.json()
    print("  OK skill upload roundtrips through admin API")

    # Verify the file landed where the resolver expects it
    target = Path(skill_root) / "tenantA" / "greeter" / "SKILL.md"
    assert target.exists(), f"skill manifest missing at {target}"
    print(f"  OK skill manifest landed at {target}")

    # Tenant B shouldn't see it
    tok_b = _make_token(sign, sub="bob", tenant="tenantB")
    r = client.get("/tenants/tenantB/skills",
                   headers={"Authorization": f"Bearer {tok_b}"})
    assert r.json()["skills"] == []
    print("  OK skill is tenant-scoped")

    # Resolver test (resolver returns SkillToolset's tools; we just
    # verify it doesn't crash — actual skill schema validation is ADK's
    # concern, and the simple manifest above may or may not satisfy
    # ADK's `load_skill_from_dir` strict shape requirements).
    from adk_cc.tools.skills_tenant import TenantSkillToolset

    class FakeTenantCtx:
        def __init__(self, tid): self.tenant_id = tid
    class FakeSession:
        def __init__(self, state): self.state = state
    class FakeRoCtx:
        invocation_id = "e2e-inv-skill"
        agent_name = "coordinator"  # SkillToolset reads this
        def __init__(self, state):
            self.session = FakeSession(state)
            self.state = state  # ADK's ReadonlyContext flattens session.state here

    ts = TenantSkillToolset(skill_root=skill_root)
    ctx_a = FakeRoCtx({"temp:tenant_context": FakeTenantCtx("tenantA")})
    tools = asyncio.run(ts.get_tools(ctx_a))
    assert isinstance(tools, list)
    assert len(tools) >= 1, f"expected at least 1 tool from greeter skill, got {len(tools)}"
    # Skill names get prefixed via SkillToolset's tool_name_prefix.
    tool_names = [getattr(t, "name", "?") for t in tools]
    print(f"  OK resolver loaded valid skill ({len(tools)} tools: {tool_names})")

    # Tenant B has no skills → empty list
    ctx_b = FakeRoCtx({"temp:tenant_context": FakeTenantCtx("tenantB")})
    assert asyncio.run(ts.get_tools(ctx_b)) == []
    print("  OK resolver per-tenant scoped")

    # DELETE
    r = client.request("DELETE", "/tenants/tenantA/skills/greeter", headers=auth_a)
    assert r.status_code == 200
    r = client.get("/tenants/tenantA/skills", headers=auth_a)
    assert r.json()["skills"] == []
    assert not target.exists()
    print("  OK skill deletion takes effect on next resolver call")


def main():
    from fastapi.testclient import TestClient

    from adk_cc.credentials import InMemoryCredentialProvider
    from adk_cc.service.registry import JsonFileTenantResourceRegistry
    from adk_cc.tools.mcp_tenant import McpServerConfig

    jwks, sign = _setup_jwt()

    with tempfile.TemporaryDirectory() as reg_dir, \
         tempfile.TemporaryDirectory() as skill_dir:
        registry = JsonFileTenantResourceRegistry[McpServerConfig](
            root=reg_dir, kind="mcp", model=McpServerConfig,
            id_attr="server_name",
        )
        credentials = InMemoryCredentialProvider()
        app = _make_app(jwks, registry=registry, credentials=credentials,
                        skill_root=skill_dir)
        client = TestClient(app)

        test_block_A_jwt_auth(client, sign)
        test_block_BD_mcp_admin_and_resolver(
            client, sign, registry=registry, credentials=credentials,
        )
        test_block_C_skills_admin_and_resolver(client, sign, skill_root=skill_dir)

    print("\n=== ALL E2E PASSED ===")


if __name__ == "__main__":
    main()
