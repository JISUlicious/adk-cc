"""Access requests (user-initiated joins, the mirror of invites): request →
pending (login hard-blocked) → admin approve/reject. Model-free, pure
service/provider calls — nothing executes.

Run: .venv/bin/python tests/test_access_requests.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.identity.provider import AccountPendingError, EmailPasswordProvider
from adk_cc.identity.service import IdentityService
from adk_cc.identity.store import JsonFileInviteStore, JsonFileUserStore
from adk_cc.identity.tokens import TokenIssuer


def _svc(mode: str = "single", access_requests: bool = True) -> IdentityService:
    d = tempfile.mkdtemp(prefix="idreq-")
    store = JsonFileUserStore(os.path.join(d, "users.json"))
    issuer = TokenIssuer(key_path=os.path.join(d, "k.json"))
    provider = EmailPasswordProvider(store, mode=mode, global_tenant_id="local",
                                     admin_role="admin", access_requests=access_requests)
    invites = JsonFileInviteStore(os.path.join(d, "invites.json"))
    return IdentityService(provider=provider, issuer=issuer, mode=mode, invites=invites)


def _request(svc, email="jane@example.com", note="hi, QA team"):
    return asyncio.run(svc.provider.request_access(
        email=email, password="password123", name="Jane", note=note))


def test_capability_single_on_multi_off():
    assert _svc("single").provider.describe()["access_requests"] is True
    assert _svc("multi").provider.describe()["access_requests"] is False
    assert _svc("single", access_requests=False).provider.describe()["access_requests"] is False


def test_request_creates_pending_on_global_tenant():
    svc = _svc()
    ident = _request(svc)
    rec = svc.store.get(ident.user_id)
    assert rec.status == "pending" and rec.tenant_id == "local"
    assert rec.roles == [] and rec.note == "hi, QA team"


def test_pending_login_hard_blocked_with_distinct_signal():
    svc = _svc()
    _request(svc)
    # correct password → the distinct pending signal (not a token, not None)
    try:
        asyncio.run(svc.provider.login_password("jane@example.com", "password123"))
        assert False, "pending account must not log in"
    except AccountPendingError:
        pass
    # wrong password → flat None (no status leak to probers)
    assert asyncio.run(svc.provider.login_password("jane@example.com", "wrong-pass")) is None


def test_pending_hidden_from_members_listed_as_request():
    svc = _svc()
    svc.provider.provision(email="admin@local.io", password="password123",
                           tenant_id="local", roles=["admin"])
    ident = _request(svc)
    assert all(m["id"] != ident.user_id for m in svc.list_members("local"))
    reqs = svc.list_access_requests("local")
    assert [r["id"] for r in reqs] == [ident.user_id] and reqs[0]["note"] == "hi, QA team"


def test_approve_activates_member_and_login_works():
    svc = _svc()
    ident = _request(svc)
    m = svc.approve_access_request("local", ident.user_id)
    assert m["status"] == "active" and m["roles"] == ["member"]
    logged = asyncio.run(svc.provider.login_password("jane@example.com", "password123"))
    assert logged is not None and logged.tenant_id == "local"
    assert svc.list_access_requests("local") == []
    # double-approve is refused
    try:
        svc.approve_access_request("local", ident.user_id)
        assert False, "approving a non-pending account must be refused"
    except ValueError:
        pass


def test_reject_deletes_record_and_email_is_free_again():
    svc = _svc()
    ident = _request(svc)
    svc.reject_access_request("local", ident.user_id)
    assert svc.store.get(ident.user_id) is None
    assert asyncio.run(svc.provider.login_password("jane@example.com", "password123")) is None
    _request(svc)  # same email can request again after a rejection


def test_tenant_scoping_admin_of_other_org_cannot_touch():
    svc = _svc()
    ident = _request(svc)
    for op in (svc.approve_access_request, svc.reject_access_request):
        try:
            op("beta", ident.user_id)
            assert False, "cross-tenant request access must 404"
        except KeyError:
            pass


def test_disabled_requests_and_multi_mode_refused():
    for svc in (_svc("single", access_requests=False), _svc("multi")):
        try:
            _request(svc)
            assert False, "request_access must be refused when unsupported"
        except PermissionError:
            pass


def test_duplicate_email_refused():
    svc = _svc()
    svc.provider.provision(email="jane@example.com", password="password123",
                           tenant_id="local", roles=[])
    try:
        _request(svc)
        assert False, "duplicate email must be refused"
    except ValueError:
        pass


def test_approve_reject_only_touch_pending():
    svc = _svc()
    active = svc.provider.provision(email="bob@local.io", password="password123",
                                    tenant_id="local", roles=["member"])
    for op in (svc.approve_access_request, svc.reject_access_request):
        try:
            op("local", active.user_id)
            assert False, "active members must not be approvable/rejectable"
        except ValueError:
            pass
    assert svc.store.get(active.user_id) is not None


def test_enable_disable_cannot_activate_a_pending_request():
    # A pending request must go through approve (which assigns a role + audits),
    # not the enable/disable status endpoint — else it activates with roles=[].
    svc = _svc()
    ident = _request(svc)
    for status in ("active", "disabled"):
        try:
            svc.set_member_status("local", ident.user_id, status)
            assert False, "set_member_status must refuse a pending request"
        except ValueError:
            pass
    # still pending, still roleless, still in the queue
    rec = svc.store.get(ident.user_id)
    assert rec.status == "pending" and rec.roles == []
    assert [r["id"] for r in svc.list_access_requests("local")] == [ident.user_id]


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
    print("\nall access-request tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
