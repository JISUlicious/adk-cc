"""Password reset via admin-minted one-time links: create → complete →
single-use; expiry; tenant scoping; sessions revoked. Model-free, pure
service calls — nothing executes.

Run: .venv/bin/python tests/test_password_reset.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.identity.provider import EmailPasswordProvider
from adk_cc.identity.service import IdentityService
from adk_cc.identity.store import (
    JsonFileRefreshTokenStore,
    JsonFileResetTokenStore,
    JsonFileUserStore,
)
from adk_cc.identity.tokens import TokenIssuer


def _svc(reset_ttl_s: int = 3600) -> IdentityService:
    d = tempfile.mkdtemp(prefix="idpr-")
    store = JsonFileUserStore(os.path.join(d, "users.json"))
    issuer = TokenIssuer(key_path=os.path.join(d, "k.json"))
    provider = EmailPasswordProvider(store, mode="single", global_tenant_id="local",
                                     admin_role="admin")
    return IdentityService(
        provider=provider, issuer=issuer, mode="single",
        refresh=JsonFileRefreshTokenStore(os.path.join(d, "refresh.json")),
        resets=JsonFileResetTokenStore(os.path.join(d, "resets.json")),
        reset_ttl_s=reset_ttl_s)


def _member(svc, email="m@local.io"):
    return svc.provider.provision(email=email, password="password123",
                                  tenant_id="local", roles=["member"])


def _login(svc, email, password):
    return asyncio.run(svc.provider.login_password(email, password))


def test_create_complete_and_login_with_new_password():
    svc = _svc()
    u = _member(svc)
    raw = svc.create_password_reset("local", u.user_id)
    assert svc.reset_public(raw) == {"email": "m@local.io", "name": ""}
    ident = svc.complete_password_reset(raw, "newpassword1")
    assert ident.user_id == u.user_id
    assert _login(svc, "m@local.io", "newpassword1") is not None
    assert _login(svc, "m@local.io", "password123") is None  # old one dead


def test_single_use():
    svc = _svc()
    u = _member(svc)
    raw = svc.create_password_reset("local", u.user_id)
    svc.complete_password_reset(raw, "newpassword1")
    assert svc.reset_public(raw) is None
    try:
        svc.complete_password_reset(raw, "another-pass1")
        assert False, "a consumed reset link must not work twice"
    except ValueError:
        pass


def test_short_password_rejected_without_consuming_token():
    svc = _svc()
    u = _member(svc)
    raw = svc.create_password_reset("local", u.user_id)
    try:
        svc.complete_password_reset(raw, "short")
        assert False, "short password must be rejected"
    except ValueError:
        pass
    # token survived the failed attempt and still works
    svc.complete_password_reset(raw, "longenough1")
    assert _login(svc, "m@local.io", "longenough1") is not None


def test_expired_link_rejected():
    svc = _svc(reset_ttl_s=-1)
    u = _member(svc)
    raw = svc.create_password_reset("local", u.user_id)
    assert svc.reset_public(raw) is None
    try:
        svc.complete_password_reset(raw, "newpassword1")
        assert False, "expired link must be rejected"
    except ValueError:
        pass


def test_tenant_scoping_and_pending_refused():
    svc = _svc()
    u = _member(svc)
    try:
        svc.create_password_reset("beta", u.user_id)
        assert False, "cross-tenant reset must 404"
    except KeyError:
        pass
    pending = asyncio.run(svc.provider.request_access(
        email="p@local.io", password="password123"))
    try:
        svc.create_password_reset("local", pending.user_id)
        assert False, "pending accounts have no reset flow"
    except ValueError:
        pass


def test_reset_revokes_refresh_sessions():
    svc = _svc()
    u = _member(svc)
    rt = svc.issue_refresh_token(u.user_id)
    raw = svc.create_password_reset("local", u.user_id)
    svc.complete_password_reset(raw, "newpassword1")
    try:
        svc.rotate_refresh_token(rt)
        assert False, "reset must revoke existing sessions"
    except ValueError:
        pass


def test_no_reset_store_refuses():
    svc = _svc()
    bare = IdentityService(provider=svc.provider, issuer=svc.issuer, mode="single")
    u = _member(svc)
    try:
        bare.create_password_reset("local", u.user_id)
        assert False, "no store → resets unavailable"
    except ValueError:
        pass
    assert bare.reset_public("anything") is None


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
    print("\nall password-reset tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
