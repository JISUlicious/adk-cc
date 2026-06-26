"""Two stock CredentialProvider impls.

`InMemoryCredentialProvider` — dev/tests; lost on restart.

`EncryptedFileCredentialProvider` — single-host on-prem; one file per
`(tenant, key)` under `<root>/<tenant_id>/<key>.enc`, encrypted with
`cryptography.fernet`. The Fernet key comes from `ADK_CC_CREDENTIAL_KEY`
or the constructor; generate one with:

    python -c "from cryptography.fernet import Fernet; \
        print(Fernet.generate_key().decode())"

Operators wanting Vault / AWS Secrets Manager / K8s secrets / GCP Secret
Manager implement `CredentialProvider` themselves and pass it to the
server factory. The two impls here cover dev and single-host on-prem.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from filelock import FileLock

from .provider import CredentialProvider


class InMemoryCredentialProvider(CredentialProvider):
    """Dev/test credential store, lost on restart.

    The backing dict is a PROCESS-WIDE singleton (shared across all
    instances) so the agent's tenant toolset and the admin-panel routes —
    which each construct their own provider — observe the same secrets in a
    single-process dev deployment. Encrypted-file is file-backed and shares
    state inherently; in-memory needs this to match that behavior. Pass
    `shared=False` for an isolated store (tests).
    """

    # Keyed by (tenant_id, user_id_or_"", key). user_id "" is the tenant-shared
    # scope; a real user_id is that user's personal scope.
    _SHARED_STORE: dict[tuple[str, str, str], str] = {}

    def __init__(self, *, shared: bool = True) -> None:
        self._store: dict[tuple[str, str, str], str] = (
            InMemoryCredentialProvider._SHARED_STORE if shared else {}
        )

    async def get(
        self, *, tenant_id: str, key: str, user_id: str | None = None
    ) -> str | None:
        if user_id:
            v = self._store.get((tenant_id, user_id, key))
            if v is not None:
                return v  # personal value wins
        return self._store.get((tenant_id, "", key))  # tenant-shared fallback

    async def put(
        self, *, tenant_id: str, key: str, value: str, user_id: str | None = None
    ) -> None:
        self._store[(tenant_id, user_id or "", key)] = value

    async def delete(
        self, *, tenant_id: str, key: str, user_id: str | None = None
    ) -> None:
        self._store.pop((tenant_id, user_id or "", key), None)

    async def list_keys(
        self, *, tenant_id: str, user_id: str | None = None
    ) -> list[str]:
        scope = user_id or ""
        return sorted(k for (t, u, k) in self._store if t == tenant_id and u == scope)


class EncryptedFileCredentialProvider(CredentialProvider):
    def __init__(self, *, root: str, key: Optional[str] = None) -> None:
        from cryptography.fernet import Fernet

        if key is None:
            key = os.environ.get("ADK_CC_CREDENTIAL_KEY")
        if not key:
            raise RuntimeError(
                "EncryptedFileCredentialProvider needs a Fernet key — pass "
                "key=... or set ADK_CC_CREDENTIAL_KEY. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        self._root = Path(root)

    @staticmethod
    def _safe_component(value: str, label: str) -> str:
        # Allow basic id-shaped strings; reject anything that could
        # traverse the filesystem. Tenants and keys come from
        # operator-controlled identifiers, not free text.
        safe = "".join(c for c in value if c.isalnum() or c in "-_")
        if safe != value or not safe:
            raise ValueError(f"unsafe {label} for filesystem path: {value!r}")
        return safe

    # Reserved subdir under a tenant that holds per-user scopes. A tenant-shared
    # key can't be named this (it would be a dir, not a `<key>.enc` file, so no
    # real collision — but reserving it keeps the layout unambiguous).
    _USERS_DIR = "_users"

    def _scope_dir(self, tenant_id: str, user_id: str | None) -> Path:
        t = self._safe_component(tenant_id, "tenant_id")
        if user_id:
            u = self._safe_component(user_id, "user_id")
            return self._root / t / self._USERS_DIR / u
        return self._root / t

    def _path(self, tenant_id: str, key: str, user_id: str | None = None) -> Path:
        k = self._safe_component(key, "credential key")
        if user_id is None and k == self._USERS_DIR:
            raise ValueError(f"credential key {key!r} is reserved")
        return self._scope_dir(tenant_id, user_id) / f"{k}.enc"

    def _read_path(self, p: Path) -> Optional[str]:
        if not p.exists():
            return None
        with FileLock(str(p) + ".lock"):
            blob = p.read_bytes()
        return self._fernet.decrypt(blob).decode("utf-8")

    async def get(
        self, *, tenant_id: str, key: str, user_id: str | None = None
    ) -> str | None:
        user_p = self._path(tenant_id, key, user_id) if user_id else None
        shared_p = self._path(tenant_id, key, None)

        def _read() -> Optional[str]:
            if user_p is not None:
                v = self._read_path(user_p)
                if v is not None:
                    return v  # personal value wins
            return self._read_path(shared_p)  # tenant-shared fallback

        return await asyncio.to_thread(_read)

    async def put(
        self, *, tenant_id: str, key: str, value: str, user_id: str | None = None
    ) -> None:
        p = self._path(tenant_id, key, user_id)

        def _write() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            blob = self._fernet.encrypt(value.encode("utf-8"))
            with FileLock(str(p) + ".lock"):
                tmp = p.with_suffix(p.suffix + ".tmp")
                tmp.write_bytes(blob)
                tmp.replace(p)

        await asyncio.to_thread(_write)

    async def delete(
        self, *, tenant_id: str, key: str, user_id: str | None = None
    ) -> None:
        p = self._path(tenant_id, key, user_id)

        def _delete() -> None:
            with FileLock(str(p) + ".lock"):
                if p.exists():
                    p.unlink()

        await asyncio.to_thread(_delete)

    async def list_keys(
        self, *, tenant_id: str, user_id: str | None = None
    ) -> list[str]:
        scope_dir = self._scope_dir(tenant_id, user_id)

        def _list() -> list[str]:
            if not scope_dir.is_dir():
                return []
            # One file per key: `<key>.enc`. Strip the suffix; ignore the
            # sibling `.lock` files (and the `_users` subdir for the shared scope).
            return sorted(
                p.name[: -len(".enc")]
                for p in scope_dir.iterdir()
                if p.is_file() and p.name.endswith(".enc")
            )

        return await asyncio.to_thread(_list)
