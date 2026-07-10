"""Refresh tokens: issue → rotate-on-use → reuse detection kills the chain;
revocation on logout / password change / disable; expiry. Model-free, pure
service calls — nothing executes.

Run: .venv/bin/python tests/test_refresh_tokens.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.identity.provider import EmailPasswordProvider
from adk_cc.identity.service import IdentityService
from adk_cc.identity.store import JsonFileRefreshTokenStore, JsonFileUserStore
from adk_cc.identity.tokens import TokenIssuer


def _svc(refresh_ttl_s: int = 3600) -> IdentityService:
    d = tempfile.mkdtemp(prefix="idrt-")
    store = JsonFileUserStore(os.path.join(d, "users.json"))
    issuer = TokenIssuer(key_path=os.path.join(d, "k.json"))
    provider = EmailPasswordProvider(store, mode="single", global_tenant_id="local",
                                     admin_role="admin")
    refresh = JsonFileRefreshTokenStore(os.path.join(d, "refresh_tokens.json"))
    return IdentityService(provider=provider, issuer=issuer, mode="single",
                           refresh=refresh, refresh_ttl_s=refresh_ttl_s)


def _user(svc, email="u@local.io"):
    return svc.provider.provision(email=email, password="password123",
                                  tenant_id="local", roles=["member"])


def _expect_invalid(svc, raw, label):
    try:
        svc.rotate_refresh_token(raw)
        raise AssertionError(label)
    except ValueError:
        pass


def test_issue_and_rotate_returns_identity_and_new_token():
    svc = _svc()
    u = _user(svc)
    t1 = svc.issue_refresh_token(u.user_id)
    assert t1 and svc._refresh_hash(t1) != t1  # raw never equals what's stored
    ident, t2 = svc.rotate_refresh_token(t1)
    assert ident.user_id == u.user_id and ident.tenant_id == "local"
    assert t2 and t2 != t1


def test_reuse_of_rotated_token_kills_the_chain():
    svc = _svc()
    u = _user(svc)
    t1 = svc.issue_refresh_token(u.user_id)
    _, t2 = svc.rotate_refresh_token(t1)
    _, t3 = svc.rotate_refresh_token(t2)
    # replaying t1 (already rotated) = theft signal → t3 must die too
    _expect_invalid(svc, t1, "rotated token must not rotate again")
    _expect_invalid(svc, t3, "chain tip must be revoked after reuse")


def test_logout_revokes_and_no_token_store_is_noop():
    svc = _svc()
    u = _user(svc)
    t1 = svc.issue_refresh_token(u.user_id)
    svc.revoke_refresh_token(t1)
    _expect_invalid(svc, t1, "revoked token must not rotate")
    svc.revoke_refresh_token("bogus")  # unknown token: silent no-op
    # a service without a refresh store issues "" and refuses rotation
    bare = IdentityService(provider=svc.provider, issuer=svc.issuer, mode="single")
    assert bare.issue_refresh_token(u.user_id) == ""
    _expect_invalid(bare, "anything", "no store → no rotation")


def test_expired_refresh_is_rejected():
    svc = _svc(refresh_ttl_s=-1)  # born expired
    u = _user(svc)
    t1 = svc.issue_refresh_token(u.user_id)
    _expect_invalid(svc, t1, "expired refresh must be rejected")


def test_password_change_revokes_all_sessions():
    svc = _svc()
    u = _user(svc)
    t1 = svc.issue_refresh_token(u.user_id)
    t2 = svc.issue_refresh_token(u.user_id)  # second device
    svc.change_password(u.user_id, current="password123", new="password456")
    _expect_invalid(svc, t1, "password change must revoke session 1")
    _expect_invalid(svc, t2, "password change must revoke session 2")


def test_disable_revokes_and_inactive_user_cannot_rotate():
    svc = _svc()
    admin = svc.provider.provision(email="a@local.io", password="password123",
                                   tenant_id="local", roles=["admin"])
    u = _user(svc)
    t1 = svc.issue_refresh_token(u.user_id)
    svc.set_member_status("local", u.user_id, "disabled")
    _expect_invalid(svc, t1, "disable must revoke refresh tokens")
    # re-enable, then a fresh token works again
    svc.set_member_status("local", u.user_id, "active")
    t2 = svc.issue_refresh_token(u.user_id)
    ident, _ = svc.rotate_refresh_token(t2)
    assert ident.user_id == u.user_id
    assert admin  # silence unused


def test_expired_records_pruned_from_disk():
    d = tempfile.mkdtemp(prefix="idrt-")
    store = JsonFileRefreshTokenStore(os.path.join(d, "r.json"))
    from adk_cc.identity.models import RefreshTokenRecord

    store.create(RefreshTokenRecord(id="dead", user_id="u", expires=time.time() - 10))
    store.create(RefreshTokenRecord(id="live", user_id="u", expires=time.time() + 60))
    assert store.get("dead") is None  # pruned by the second write
    assert store.get("live") is not None


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
    print("\nall refresh-token tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
