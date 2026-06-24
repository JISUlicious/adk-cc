"""Org / team management (Phase 3): members, invites, roles, tenant isolation,
last-admin guard. Model-free.

Run: .venv/bin/python tests/test_identity_org.py
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
from adk_cc.identity.store import JsonFileInviteStore, JsonFileUserStore
from adk_cc.identity.tokens import TokenIssuer


def _svc(mode: str = "multi") -> IdentityService:
    d = tempfile.mkdtemp(prefix="idorg-")
    store = JsonFileUserStore(os.path.join(d, "users.json"))
    issuer = TokenIssuer(key_path=os.path.join(d, "k.json"))
    provider = EmailPasswordProvider(store, mode=mode, global_tenant_id="local", admin_role="admin")
    invites = JsonFileInviteStore(os.path.join(d, "invites.json"))
    return IdentityService(provider=provider, issuer=issuer, mode=mode, invites=invites)


def _owner(svc: IdentityService, tenant="acme"):
    # provision an admin/owner directly into a tenant
    return svc.provider.provision(email="owner@acme.io", password="password123",
                                  tenant_id=tenant, roles=["admin"])


def test_list_members_and_tenant_isolation():
    svc = _svc()
    _owner(svc, "acme")
    svc.provider.provision(email="x@beta.io", password="password123", tenant_id="beta", roles=["admin"])
    acme = svc.list_members("acme")
    assert [m["email"] for m in acme] == ["owner@acme.io"]  # beta member NOT visible
    assert all(m["email"] != "x@beta.io" for m in acme)


def test_invite_then_accept_creates_member():
    svc = _svc()
    _owner(svc, "acme")
    inv = svc.create_invite("acme", "Member@Acme.io", role="member")
    assert svc.invite_public(inv.token)["org"] == "acme"
    ident = svc.accept_invite(inv.token, password="password123", name="Mem")
    assert ident.tenant_id == "acme" and ident.roles == ("member",)
    # now a member of acme; can log in
    assert len(svc.list_members("acme")) == 2
    # token re-use is refused
    try:
        svc.accept_invite(inv.token, password="password123")
        assert False, "used invite must not be reusable"
    except ValueError:
        pass


def test_invite_rejects_existing_member_and_bad_role():
    svc = _svc()
    _owner(svc, "acme")
    try:
        svc.create_invite("acme", "owner@acme.io")  # already a member
        assert False
    except ValueError:
        pass
    try:
        svc.create_invite("acme", "new@acme.io", role="superuser")  # invalid role
        assert False
    except ValueError:
        pass


def test_revoke_invite_blocks_accept():
    svc = _svc()
    _owner(svc, "acme")
    inv = svc.create_invite("acme", "later@acme.io")
    svc.revoke_invite("acme", inv.token)
    assert svc.invite_public(inv.token) is None
    try:
        svc.accept_invite(inv.token, password="password123")
        assert False, "revoked invite must not accept"
    except ValueError:
        pass


def test_role_change_and_disable():
    svc = _svc()
    _owner(svc, "acme")
    inv = svc.create_invite("acme", "m@acme.io")
    mem = svc.accept_invite(inv.token, password="password123")
    # promote member → admin, then disable
    svc.set_member_role("acme", mem.user_id, "admin")
    assert "admin" in svc._member_in_tenant("acme", mem.user_id).roles
    svc.set_member_status("acme", mem.user_id, "disabled")
    assert svc._member_in_tenant("acme", mem.user_id).status == "disabled"


def test_last_admin_guard():
    svc = _svc()
    owner = _owner(svc, "acme")
    # only admin → can't demote or disable
    try:
        svc.set_member_role("acme", owner.user_id, "member")
        assert False, "demoting the last admin must be refused"
    except ValueError:
        pass
    try:
        svc.set_member_status("acme", owner.user_id, "disabled")
        assert False, "disabling the last admin must be refused"
    except ValueError:
        pass
    # add a second admin → now demotion is allowed
    inv = svc.create_invite("acme", "a2@acme.io", role="admin")
    svc.accept_invite(inv.token, password="password123")
    svc.set_member_role("acme", owner.user_id, "member")  # ok now
    assert svc._member_in_tenant("acme", owner.user_id).roles == ["member"]


def test_cross_tenant_member_ops_rejected():
    svc = _svc()
    _owner(svc, "acme")
    beta_admin = svc.provider.provision(email="b@beta.io", password="password123",
                                        tenant_id="beta", roles=["admin"])
    # acme admin cannot touch a beta member (service scopes by tenant)
    try:
        svc.set_member_role("acme", beta_admin.user_id, "member")
        assert False, "cross-tenant role change must be refused"
    except KeyError:
        pass


def test_owner_is_protected():
    svc = _svc()
    owner = svc.provider.provision(email="o@acme.io", password="password123",
                                   tenant_id="acme", roles=["owner", "admin"])
    # also add a second admin so the last-admin guard isn't what trips
    svc.provider.provision(email="a2@acme.io", password="password123",
                           tenant_id="acme", roles=["admin"])
    try:
        svc.set_member_role("acme", owner.user_id, "member")
        assert False, "owner role must not be changeable"
    except ValueError:
        pass
    try:
        svc.set_member_status("acme", owner.user_id, "disabled")
        assert False, "owner must not be disable-able"
    except ValueError:
        pass


def test_multi_signup_creator_is_owner():
    svc = _svc(mode="multi")
    ident = asyncio.run(svc.provider.register(email="founder@acme.io",
                                              password="password123", org="Acme"))
    assert "owner" in ident.roles and "admin" in ident.roles


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
    print("\nall identity-org tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
