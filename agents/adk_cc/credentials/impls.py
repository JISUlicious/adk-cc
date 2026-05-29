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
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    async def get(self, *, tenant_id: str, key: str) -> str | None:
        return self._store.get((tenant_id, key))

    async def put(self, *, tenant_id: str, key: str, value: str) -> None:
        self._store[(tenant_id, key)] = value

    async def delete(self, *, tenant_id: str, key: str) -> None:
        self._store.pop((tenant_id, key), None)


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

    def _path(self, tenant_id: str, key: str) -> Path:
        t = self._safe_component(tenant_id, "tenant_id")
        k = self._safe_component(key, "credential key")
        return self._root / t / f"{k}.enc"

    async def get(self, *, tenant_id: str, key: str) -> str | None:
        p = self._path(tenant_id, key)

        def _read() -> Optional[str]:
            if not p.exists():
                return None
            with FileLock(str(p) + ".lock"):
                blob = p.read_bytes()
            return self._fernet.decrypt(blob).decode("utf-8")

        return await asyncio.to_thread(_read)

    async def put(self, *, tenant_id: str, key: str, value: str) -> None:
        p = self._path(tenant_id, key)

        def _write() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            blob = self._fernet.encrypt(value.encode("utf-8"))
            with FileLock(str(p) + ".lock"):
                tmp = p.with_suffix(p.suffix + ".tmp")
                tmp.write_bytes(blob)
                tmp.replace(p)

        await asyncio.to_thread(_write)

    async def delete(self, *, tenant_id: str, key: str) -> None:
        p = self._path(tenant_id, key)

        def _delete() -> None:
            with FileLock(str(p) + ".lock"):
                if p.exists():
                    p.unlink()

        await asyncio.to_thread(_delete)
