"""ADK_CC_ALLOW_NO_AUTH grants EVERY grade — admin included (user report:
the settings panel 404'd on all admin sections in no-auth test mode).

The boundaries pinned here matter more than the allowance:
  - the escape applies ONLY when there is no principal — a real
    authenticated non-admin is still 403'd even with the flag on;
  - without the flag, no principal stays 401 (fail-closed unchanged);
  - an explicit ADK_CC_ADMIN_PANEL=0 beats the flag (operator said no).

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_no_auth_admin.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
for _k in ("ADK_CC_ALLOW_NO_AUTH", "ADK_CC_ADMIN_PANEL"):
    os.environ.pop(_k, None)

from starlette.requests import Request  # noqa: E402,F401 — get_type_hints

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _admin_app(tmp, *, with_auth_middleware: bool):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from adk_cc.credentials import InMemoryCredentialProvider
    from adk_cc.service.admin_routes import mount_tenant_admin
    from adk_cc.service.auth import (
        AuthPrincipal, BearerTokenExtractor, make_auth_middleware,
    )
    from adk_cc.service.registry import JsonFileTenantResourceRegistry
    from adk_cc.service.server import _make_admin_role_extractor
    from adk_cc.tools.mcp_tenant import McpServerConfig

    os.environ["ADK_CC_GLOBAL_TENANT_ID"] = "local"
    app = FastAPI()
    mount_tenant_admin(
        app,
        registry=JsonFileTenantResourceRegistry[McpServerConfig](
            root=os.path.join(tmp, "registry"), kind="mcp",
            model=McpServerConfig, id_attr="server_name"),
        credentials=InMemoryCredentialProvider(shared=False),
        admin_extractor=_make_admin_role_extractor(),
    )
    if with_auth_middleware:
        app.add_middleware(make_auth_middleware(BearerTokenExtractor({
            "usertok": AuthPrincipal("bob", "local", frozenset()),
            "admintok": AuthPrincipal("alice", "local", frozenset({"admin"})),
        })))
    return TestClient(app)


def main() -> int:
    from adk_cc.service.server import _admin_enabled

    # ---- the mount gate ---------------------------------------------------
    check("panel off by default", not _admin_enabled())
    os.environ["ADK_CC_ALLOW_NO_AUTH"] = "1"
    check("no-auth flag turns the panel on", _admin_enabled())
    os.environ["ADK_CC_ADMIN_PANEL"] = "0"
    check("explicit ADK_CC_ADMIN_PANEL=0 beats the flag", not _admin_enabled())
    os.environ.pop("ADK_CC_ADMIN_PANEL", None)
    os.environ.pop("ADK_CC_ALLOW_NO_AUTH", None)

    # ---- authorize: fail-closed without the flag --------------------------
    with tempfile.TemporaryDirectory() as tmp:
        c = _admin_app(tmp, with_auth_middleware=False)
        r = c.get("/tenants/local/mcp-servers")
        check("no principal + no flag → 401", r.status_code == 401,
              r.status_code)

        os.environ["ADK_CC_ALLOW_NO_AUTH"] = "1"
        try:
            r = c.get("/tenants/local/mcp-servers")
            check("no principal + flag → allowed", r.status_code == 200,
                  r.status_code)
        finally:
            os.environ.pop("ADK_CC_ALLOW_NO_AUTH", None)

    # ---- the escape never elevates a REAL principal -----------------------
    with tempfile.TemporaryDirectory() as tmp:
        c = _admin_app(tmp, with_auth_middleware=True)
        os.environ["ADK_CC_ALLOW_NO_AUTH"] = "1"
        try:
            r = c.get("/tenants/local/mcp-servers",
                      headers={"Authorization": "Bearer usertok"})
            check("authenticated NON-admin stays 403 even with the flag",
                  r.status_code == 403, r.status_code)
            r = c.get("/tenants/local/mcp-servers",
                      headers={"Authorization": "Bearer admintok"})
            check("authenticated admin still works", r.status_code == 200,
                  r.status_code)
        finally:
            os.environ.pop("ADK_CC_ALLOW_NO_AUTH", None)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
