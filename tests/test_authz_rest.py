"""Tests for the REST AuthZ PEP (trust-the-path gate).

Builds a tiny FastAPI app with the auth + authz middlewares and drives it
with TestClient — verifying same-user 200, cross-user 403, unauth 401,
role-granted cross-user, and inert-when-disabled.

Hand-rolled (no pytest), runnable with the venv python.
"""

from __future__ import annotations

import os

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from fastapi import FastAPI
from starlette.testclient import TestClient

from adk_cc.authz import AbacPolicy, AbacPolicyDecisionPoint
from adk_cc.service.auth import BearerTokenExtractor, make_auth_middleware
from adk_cc.service.authz_routes import make_authz_middleware


def _build_app(pdp):
    app = FastAPI()

    @app.get("/apps/adk_cc/users/{user_id}/sessions/{sid}")
    async def get_session(user_id: str, sid: str):
        return {"user_id": user_id, "sid": sid}

    @app.get("/list-apps")
    async def list_apps():
        return ["adk_cc"]

    # Tokens: alice@acme (no roles), bob@beta, super@acme:admin.
    tokens = {
        "alice": ("alice", "acme"),
        "bob": ("bob", "beta"),
    }
    from adk_cc.service.auth import AuthPrincipal
    tokmap = {
        "alice": AuthPrincipal("alice", "acme"),
        "bob": AuthPrincipal("bob", "beta"),
        "super": AuthPrincipal("super", "acme", frozenset({"admin"})),
    }
    extractor = BearerTokenExtractor(tokmap)

    # authz added first (inner), auth second (outer) — same as server.py.
    app.add_middleware(make_authz_middleware(pdp))
    app.add_middleware(make_auth_middleware(extractor))
    return app


def _client(pdp=None):
    return TestClient(_build_app(pdp or AbacPolicyDecisionPoint([])))


def _h(token):
    return {"Authorization": f"Bearer {token}"}


# --- tests ----------------------------------------------------------------

def test_same_user_permitted():
    os.environ["ADK_CC_AUTHZ"] = "1"
    try:
        c = _client()
        r = c.get("/apps/adk_cc/users/alice/sessions/s1", headers=_h("alice"))
        assert r.status_code == 200, r.text
    finally:
        os.environ.pop("ADK_CC_AUTHZ", None)
    print("OK test_same_user_permitted")


def test_cross_user_forbidden():
    os.environ["ADK_CC_AUTHZ"] = "1"
    try:
        c = _client()
        # bob tries to read alice's session → 403
        r = c.get("/apps/adk_cc/users/alice/sessions/s1", headers=_h("bob"))
        assert r.status_code == 403, r.text
    finally:
        os.environ.pop("ADK_CC_AUTHZ", None)
    print("OK test_cross_user_forbidden")


def test_unauthenticated_401():
    os.environ["ADK_CC_AUTHZ"] = "1"
    try:
        c = _client()
        r = c.get("/apps/adk_cc/users/alice/sessions/s1")  # no token
        assert r.status_code == 401, r.text
    finally:
        os.environ.pop("ADK_CC_AUTHZ", None)
    print("OK test_unauthenticated_401")


def test_role_grants_cross_user():
    os.environ["ADK_CC_AUTHZ"] = "1"
    try:
        pdp = AbacPolicyDecisionPoint([
            AbacPolicy(effect="permit", roles=frozenset({"admin"}),
                       resource_type="user_data", name="admin-cross-user"),
        ])
        c = _client(pdp)
        # super@admin reads alice's data → permitted by role
        r = c.get("/apps/adk_cc/users/alice/sessions/s1", headers=_h("super"))
        assert r.status_code == 200, r.text
    finally:
        os.environ.pop("ADK_CC_AUTHZ", None)
    print("OK test_role_grants_cross_user")


def test_non_user_path_passes():
    os.environ["ADK_CC_AUTHZ"] = "1"
    try:
        c = _client()
        r = c.get("/list-apps", headers=_h("alice"))
        assert r.status_code == 200  # not a /users/ path → not gated
    finally:
        os.environ.pop("ADK_CC_AUTHZ", None)
    print("OK test_non_user_path_passes")


def test_inert_when_disabled():
    # ADK_CC_AUTHZ unset → gate off → cross-user passes (legacy path-trust).
    os.environ.pop("ADK_CC_AUTHZ", None)
    c = _client()
    r = c.get("/apps/adk_cc/users/alice/sessions/s1", headers=_h("bob"))
    assert r.status_code == 200, r.text
    print("OK test_inert_when_disabled")


if __name__ == "__main__":
    test_same_user_permitted()
    test_cross_user_forbidden()
    test_unauthenticated_401()
    test_role_grants_cross_user()
    test_non_user_path_passes()
    test_inert_when_disabled()
    print("\nall authz-rest tests passed")
