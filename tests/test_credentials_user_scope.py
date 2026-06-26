"""Per-user CredentialProvider scoping (Phase 1).

User-over-tenant layering: get(user_id=X) returns X's personal value, else falls
back to the tenant-shared value. Writes/listing are scope-EXACT. Covers both
stock impls + path-traversal safety. Model-free.

Run: .venv/bin/python tests/test_credentials_user_scope.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.credentials.impls import (  # noqa: E402
    EncryptedFileCredentialProvider,
    InMemoryCredentialProvider,
)


def _providers():
    mem = InMemoryCredentialProvider(shared=False)
    from cryptography.fernet import Fernet

    d = tempfile.mkdtemp(prefix="cred-scope-")
    enc = EncryptedFileCredentialProvider(root=d, key=Fernet.generate_key().decode())
    return [("InMemory", mem), ("EncryptedFile", enc)]


async def _run_one(name, p) -> None:
    T = "acme"
    # tenant-shared value
    await p.put(tenant_id=T, key="GITHUB_TOKEN", value="shared-tok")
    # alice overrides; bob does not
    await p.put(tenant_id=T, key="GITHUB_TOKEN", value="alice-tok", user_id="alice")

    # layering on read
    assert await p.get(tenant_id=T, key="GITHUB_TOKEN", user_id="alice") == "alice-tok", name
    assert await p.get(tenant_id=T, key="GITHUB_TOKEN", user_id="bob") == "shared-tok", name
    assert await p.get(tenant_id=T, key="GITHUB_TOKEN") == "shared-tok", name
    # absent key
    assert await p.get(tenant_id=T, key="NOPE", user_id="alice") is None, name

    # personal-only secret: visible to owner, invisible as shared / to others
    await p.put(tenant_id=T, key="PERSONAL", value="a-only", user_id="alice")
    assert await p.get(tenant_id=T, key="PERSONAL", user_id="alice") == "a-only", name
    assert await p.get(tenant_id=T, key="PERSONAL", user_id="bob") is None, name
    assert await p.get(tenant_id=T, key="PERSONAL") is None, name

    # list_keys is scope-exact
    assert await p.list_keys(tenant_id=T) == ["GITHUB_TOKEN"], (name, await p.list_keys(tenant_id=T))
    assert await p.list_keys(tenant_id=T, user_id="alice") == ["GITHUB_TOKEN", "PERSONAL"], name
    assert await p.list_keys(tenant_id=T, user_id="bob") == [], name

    # delete is scope-exact: deleting alice's override falls back to shared
    await p.delete(tenant_id=T, key="GITHUB_TOKEN", user_id="alice")
    assert await p.get(tenant_id=T, key="GITHUB_TOKEN", user_id="alice") == "shared-tok", name
    # shared still intact, tenant list unchanged
    assert await p.list_keys(tenant_id=T) == ["GITHUB_TOKEN"], name

    # cross-tenant isolation
    assert await p.get(tenant_id="other", key="GITHUB_TOKEN", user_id="alice") is None, name


def test_user_scope_both_impls():
    for name, p in _providers():
        asyncio.run(_run_one(name, p))


def test_encrypted_path_traversal_rejected():
    from cryptography.fernet import Fernet

    d = tempfile.mkdtemp(prefix="cred-trav-")
    p = EncryptedFileCredentialProvider(root=d, key=Fernet.generate_key().decode())

    async def _attempts():
        for bad_user in ["../escape", "a/b", "..", "x/../y"]:
            try:
                await p.put(tenant_id="acme", key="K", value="v", user_id=bad_user)
                raise AssertionError(f"traversal user_id accepted: {bad_user!r}")
            except ValueError:
                pass
        for bad_key in ["../k", "a/b"]:
            try:
                await p.put(tenant_id="acme", key=bad_key, value="v")
                raise AssertionError(f"traversal key accepted: {bad_key!r}")
            except ValueError:
                pass
        # reserved shared key
        try:
            await p.put(tenant_id="acme", key="_users", value="v")
            raise AssertionError("reserved key _users accepted as shared")
        except ValueError:
            pass

    asyncio.run(_attempts())


def test_backcompat_no_user_id_unchanged():
    # user_id=None behaves exactly like the original tenant-only API.
    p = InMemoryCredentialProvider(shared=False)

    async def _go():
        await p.put(tenant_id="acme", key="K", value="v")
        assert await p.get(tenant_id="acme", key="K") == "v"
        await p.delete(tenant_id="acme", key="K")
        assert await p.get(tenant_id="acme", key="K") is None

    asyncio.run(_go())


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
    print("\nall credentials-user-scope tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
