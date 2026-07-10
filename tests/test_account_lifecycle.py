"""Account lifecycle self-service: email change, deactivate, delete —
password-gated, owner/last-admin protected, credentials revoked. Model-free,
pure service calls — nothing executes.

Run: .venv/bin/python tests/test_account_lifecycle.py
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
    JsonFileApiKeyStore,
    JsonFileRefreshTokenStore,
    JsonFileUserStore,
)
from adk_cc.identity.tokens import TokenIssuer


def _svc() -> IdentityService:
    d = tempfile.mkdtemp(prefix="idal-")
    store = JsonFileUserStore(os.path.join(d, "users.json"))
    issuer = TokenIssuer(key_path=os.path.join(d, "k.json"))
    provider = EmailPasswordProvider(store, mode="single", global_tenant_id="local",
                                     admin_role="admin")
    return IdentityService(
        provider=provider, issuer=issuer, mode="single",
        api_keys=JsonFileApiKeyStore(os.path.join(d, "keys.json")),
        refresh=JsonFileRefreshTokenStore(os.path.join(d, "refresh.json")))


def _member(svc, email="m@local.io", roles=("member",)):
    return svc.provider.provision(email=email, password="password123",
                                  tenant_id="local", roles=list(roles))


def _login(svc, email, password):
    return asyncio.run(svc.provider.login_password(email, password))


def _expect_value_error(fn, label):
    try:
        fn()
        raise AssertionError(label)
    except ValueError:
        pass


def test_change_email_swaps_and_validates():
    svc = _svc()
    u = _member(svc)
    _member(svc, email="taken@local.io")
    _expect_value_error(
        lambda: svc.change_email(u.user_id, new_email="new@local.io", password="wrong"),
        "wrong password must be rejected")
    _expect_value_error(
        lambda: svc.change_email(u.user_id, new_email="not-an-email", password="password123"),
        "invalid email must be rejected")
    _expect_value_error(
        lambda: svc.change_email(u.user_id, new_email="Taken@local.io", password="password123"),
        "taken email must be rejected")
    prof = svc.change_email(u.user_id, new_email="New@Local.io", password="password123")
    assert prof["email"] == "new@local.io"  # normalized
    assert _login(svc, "new@local.io", "password123") is not None
    assert _login(svc, "m@local.io", "password123") is None


def test_deactivate_blocks_login_revokes_sessions_admin_reenables():
    svc = _svc()
    svc.provider.provision(email="a@local.io", password="password123",
                           tenant_id="local", roles=["admin"])
    u = _member(svc)
    rt = svc.issue_refresh_token(u.user_id)
    _expect_value_error(
        lambda: svc.deactivate_account(u.user_id, password="wrong"),
        "wrong password must be rejected")
    svc.deactivate_account(u.user_id, password="password123")
    assert _login(svc, "m@local.io", "password123") is None
    _expect_value_error(lambda: svc.rotate_refresh_token(rt),
                        "deactivation must revoke sessions")
    svc.set_member_status("local", u.user_id, "active")  # admin re-enable
    assert _login(svc, "m@local.io", "password123") is not None


def test_owner_and_last_admin_protected():
    svc = _svc()
    owner = _member(svc, email="o@local.io", roles=("owner", "admin"))
    _expect_value_error(lambda: svc.deactivate_account(owner.user_id, password="password123"),
                        "owner must not self-deactivate")
    _expect_value_error(lambda: svc.delete_account(owner.user_id, password="password123"),
                        "owner must not self-delete")
    # a lone (non-owner) admin is protected too
    svc2 = _svc()
    admin = _member(svc2, email="a2@local.io", roles=("admin",))
    _expect_value_error(lambda: svc2.delete_account(admin.user_id, password="password123"),
                        "last admin must not self-delete")
    # with a second admin present, deletion is allowed
    _member(svc2, email="a3@local.io", roles=("admin",))
    svc2.delete_account(admin.user_id, password="password123")
    assert svc2.store.get(admin.user_id) is None


def test_delete_revokes_pats_and_refresh_and_frees_email():
    svc = _svc()
    u = _member(svc)
    rec, _tok = svc.create_api_key(u.user_id, name="ci")
    rt = svc.issue_refresh_token(u.user_id)
    svc.delete_account(u.user_id, password="password123")
    assert svc.store.get(u.user_id) is None
    assert svc.api_keys.get(rec.id).revoked is True
    _expect_value_error(lambda: svc.rotate_refresh_token(rt),
                        "delete must revoke sessions")
    _member(svc)  # the email is free again


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
    print("\nall account-lifecycle tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
