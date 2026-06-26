"""Generic per-tenant resource registry.

Used as `TenantResourceRegistry[McpServerConfig]` for MCP server
configs and `TenantResourceRegistry[SkillRef]` for skill references.
Both share the same CRUD lifecycle; the generic ABC means operators
implementing a DB-backed registry write one impl, not two.

Default impl: `JsonFileTenantResourceRegistry` — one JSON file per
`(tenant, kind)` at `<root>/<tenant_id>/<kind>.json`. filelock-protected,
same shape as `JsonFileTaskStorage`.

Why not reuse ADK's session storage? ADK's sessions are conversation
state; this is operator-side configuration that the tenant controls
via registration endpoints. Different lifecycle, different access
pattern.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Generic, Optional, TypeVar

from filelock import FileLock
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class TenantResourceRegistry(ABC, Generic[T]):
    """Per-tenant (and optionally per-user) CRUD over a typed resource model.

    `user_id=None` is the TENANT-shared scope; a real `user_id` is that user's
    personal scope. Reads/writes are scope-EXACT; `list_union` returns the
    user-over-tenant union (see below). Mirrors the CredentialProvider scoping.
    """

    @abstractmethod
    async def list_for_tenant(
        self, tenant_id: str, user_id: Optional[str] = None
    ) -> list[T]:
        """Resources at the EXACT scope. Empty list if none."""

    @abstractmethod
    async def add(
        self, *, tenant_id: str, resource: T, user_id: Optional[str] = None
    ) -> None:
        """Add or overwrite a resource at the EXACT scope. Idempotent on `id_attr`."""

    @abstractmethod
    async def remove(
        self, *, tenant_id: str, resource_id: str, user_id: Optional[str] = None
    ) -> None:
        """Remove a resource by id at the EXACT scope. No-op if absent."""

    async def list_union(
        self, tenant_id: str, user_id: Optional[str] = None
    ) -> list[T]:
        """Tenant-shared ∪ user-personal, the user's winning on id collision.
        Default returns just the tenant scope; the stock JSON impl overrides
        with a real union (it knows `id_attr`)."""
        return await self.list_for_tenant(tenant_id)


class JsonFileTenantResourceRegistry(TenantResourceRegistry[T]):
    """One JSON file per `(tenant, kind)`. filelock-protected for multi-worker safety."""

    def __init__(
        self,
        *,
        root: str,
        kind: str,
        model: type[T],
        id_attr: str = "id",
    ) -> None:
        self._root = Path(root)
        self._kind = kind
        self._model = model
        self._id_attr = id_attr

    @staticmethod
    def _safe_component(value: str, label: str) -> str:
        safe = "".join(c for c in value if c.isalnum() or c in "-_")
        if safe != value or not safe:
            raise ValueError(f"unsafe {label} for filesystem path: {value!r}")
        return safe

    def _path(self, tenant_id: str, user_id: Optional[str] = None) -> Path:
        t = self._safe_component(tenant_id, "tenant_id")
        if user_id:
            u = self._safe_component(user_id, "user_id")
            return self._root / t / "_users" / u / f"{self._kind}.json"
        return self._root / t / f"{self._kind}.json"

    def _read_locked(self, p: Path) -> list[dict]:
        if not p.exists():
            return []
        with FileLock(str(p) + ".lock"):
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)

    def _write_locked(self, p: Path, data: list[dict]) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(p) + ".lock"):
            tmp = p.with_suffix(p.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            tmp.replace(p)

    async def list_for_tenant(
        self, tenant_id: str, user_id: Optional[str] = None
    ) -> list[T]:
        p = self._path(tenant_id, user_id)
        raw = await asyncio.to_thread(self._read_locked, p)
        return [self._model.model_validate(item) for item in raw]

    async def list_union(
        self, tenant_id: str, user_id: Optional[str] = None
    ) -> list[T]:
        tenant = await self.list_for_tenant(tenant_id)
        if not user_id:
            return tenant
        by_id = {getattr(r, self._id_attr): r for r in tenant}
        for r in await self.list_for_tenant(tenant_id, user_id):  # user wins
            by_id[getattr(r, self._id_attr)] = r
        return list(by_id.values())

    async def add(
        self, *, tenant_id: str, resource: T, user_id: Optional[str] = None
    ) -> None:
        p = self._path(tenant_id, user_id)
        new_id = getattr(resource, self._id_attr)
        new_dump = resource.model_dump(mode="json")

        def _do() -> None:
            data = self._read_locked(p)
            data = [item for item in data if item.get(self._id_attr) != new_id]
            data.append(new_dump)
            self._write_locked(p, data)

        await asyncio.to_thread(_do)

    async def remove(
        self, *, tenant_id: str, resource_id: str, user_id: Optional[str] = None
    ) -> None:
        p = self._path(tenant_id, user_id)

        def _do() -> None:
            data = self._read_locked(p)
            data = [item for item in data if item.get(self._id_attr) != resource_id]
            self._write_locked(p, data)

        await asyncio.to_thread(_do)
