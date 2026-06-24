"""In-house email+password identity: passwords, store, token issuance +
validation through the SAME JwtAuthExtractor the server uses, and the
EmailPasswordProvider's single/multi behavior. Model-free.

Run: .venv/bin/python tests/test_identity.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.identity.models import UserRecord
from adk_cc.identity.passwords import hash_password, verify_password
from adk_cc.identity.provider import EmailPasswordProvider
from adk_cc.identity.store import JsonFileUserStore
from adk_cc.identity.tokens import TokenIssuer


def _store() -> JsonFileUserStore:
    return JsonFileUserStore(os.path.join(tempfile.mkdtemp(prefix="idstore-"), "users.json"))


class _Req:
    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}


# ---------- passwords ----------
def test_password_roundtrip():
    h = hash_password("correct horse battery staple")
    assert h.startswith("scrypt$")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "garbage$nope")  # malformed → False, no raise


def test_password_salts_differ():
    assert hash_password("same") != hash_password("same")  # random per-hash salt


# ---------- store ----------
def test_store_create_get_unique():
    s = _store()
    s.create(UserRecord("u1", "A@X.IO", "ph"))
    assert s.count() == 1
    assert s.get("u1").email == "a@x.io"  # normalized on write
    assert s.get_by_email("a@x.io").user_id == "u1"
    try:
        s.create(UserRecord("u2", "a@x.io", "ph2"))
        assert False, "duplicate email should raise"
    except ValueError:
        pass
    try:
        s.create(UserRecord("u1", "b@x.io", "ph"))
        assert False, "duplicate user_id should raise"
    except ValueError:
        pass


# ---------- tokens validate via the real extractor ----------
def test_token_validates_via_extractor():
    d = tempfile.mkdtemp(prefix="idtok-")
    iss = TokenIssuer(key_path=os.path.join(d, "k.json"), issuer="adk-cc")
    tok = iss.issue(user_id="u1", tenant_id="acme", roles=("admin",), email="a@x.io")
    from adk_cc.service.auth import JwtAuthExtractor

    ex = JwtAuthExtractor(jwks=iss.public_jwks(), issuer="adk-cc")
    pr = asyncio.run(ex(_Req(tok)))
    assert pr.user_id == "u1" and pr.tenant_id == "acme" and "admin" in pr.roles


def test_token_key_persists_across_instances():
    d = tempfile.mkdtemp(prefix="idtok2-")
    iss1 = TokenIssuer(key_path=os.path.join(d, "k.json"))
    tok = iss1.issue(user_id="u", tenant_id="t")
    iss2 = TokenIssuer(key_path=os.path.join(d, "k.json"))  # reload persisted key
    from adk_cc.service.auth import JwtAuthExtractor

    ex = JwtAuthExtractor(jwks=iss2.public_jwks(), issuer="adk-cc")
    assert asyncio.run(ex(_Req(tok))).user_id == "u"


# ---------- provider: single vs multi ----------
def test_provider_single_no_signup():
    p = EmailPasswordProvider(_store(), mode="single", global_tenant_id="local")
    assert p.supports_registration is False
    try:
        asyncio.run(p.register(email="a@x.io", password="password1"))
        assert False, "single-mode register must be refused"
    except PermissionError:
        pass
    p.provision(email="a@x.io", password="password1", roles=["admin"])  # admin path
    ident = asyncio.run(p.login_password("a@x.io", "password1"))
    assert ident and ident.tenant_id == "local" and "admin" in ident.roles
    assert asyncio.run(p.login_password("a@x.io", "nope")) is None  # wrong pw


def test_provider_multi_signup_owns_tenant():
    p = EmailPasswordProvider(_store(), mode="multi", global_tenant_id="local", admin_role="admin")
    assert p.supports_registration is True
    ident = asyncio.run(p.register(email="o@x.io", password="password1", org="Acme Inc"))
    assert ident.tenant_id == "acme-inc" and "admin" in ident.roles  # owns a fresh tenant
    again = asyncio.run(p.login_password("o@x.io", "password1"))
    assert again.tenant_id == "acme-inc"
    try:
        asyncio.run(p.register(email="b@x.io", password="short"))
        assert False, "short password must be rejected"
    except ValueError:
        pass


def test_provider_login_rejects_disabled():
    s = _store()
    p = EmailPasswordProvider(s, mode="single")
    p.provision(email="d@x.io", password="password1")
    rec = s.get_by_email("d@x.io")
    rec.status = "disabled"
    s.update(rec)
    assert asyncio.run(p.login_password("d@x.io", "password1")) is None


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
    print("\nall identity tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
