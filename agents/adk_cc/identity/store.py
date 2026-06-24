"""User account storage.

`UserStore` is the abstraction seam — a DB-backed impl (Postgres, etc.) for
scale is a drop-in replacement. The default `JsonFileUserStore` keeps every
account in one filelock-protected JSON object keyed by user_id, matching the
codebase's `JsonFileTenantResourceRegistry` convention (atomic temp-file swap).
Fine for a self-hosted single deployment; swap the ABC for serious scale.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from filelock import FileLock

from .models import InviteRecord, UserRecord


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


class UserStore(ABC):
    @abstractmethod
    def get(self, user_id: str) -> UserRecord | None: ...

    @abstractmethod
    def get_by_email(self, email: str) -> UserRecord | None: ...

    @abstractmethod
    def create(self, record: UserRecord) -> None:
        """Insert. Raises ValueError if the user_id or email already exists."""

    @abstractmethod
    def update(self, record: UserRecord) -> None: ...

    @abstractmethod
    def list_by_tenant(self, tenant_id: str) -> list[UserRecord]:
        """Every account in a tenant/org (the org's members)."""

    @abstractmethod
    def count(self) -> int: ...


class JsonFileUserStore(UserStore):
    """All accounts in one JSON object {user_id: record}, filelock-protected."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = FileLock(str(self._path) + ".lock")

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        with self._path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self._path)

    def get(self, user_id: str) -> UserRecord | None:
        with self._lock:
            d = self._read().get(user_id)
        return UserRecord.from_dict(d) if d else None

    def get_by_email(self, email: str) -> UserRecord | None:
        e = normalize_email(email)
        with self._lock:
            for d in self._read().values():
                if d.get("email") == e:
                    return UserRecord.from_dict(d)
        return None

    def create(self, record: UserRecord) -> None:
        record.email = normalize_email(record.email)
        with self._lock:
            data = self._read()
            if record.user_id in data:
                raise ValueError("user_id already exists")
            if any(d.get("email") == record.email for d in data.values()):
                raise ValueError("email already registered")
            data[record.user_id] = record.to_dict()
            self._write(data)

    def update(self, record: UserRecord) -> None:
        record.email = normalize_email(record.email)
        with self._lock:
            data = self._read()
            data[record.user_id] = record.to_dict()
            self._write(data)

    def list_by_tenant(self, tenant_id: str) -> list[UserRecord]:
        with self._lock:
            return [
                UserRecord.from_dict(d)
                for d in self._read().values()
                if d.get("tenant_id") == tenant_id
            ]

    def count(self) -> int:
        with self._lock:
            return len(self._read())


class InviteStore(ABC):
    @abstractmethod
    def create(self, invite: InviteRecord) -> None: ...

    @abstractmethod
    def get(self, token: str) -> InviteRecord | None: ...

    @abstractmethod
    def update(self, invite: InviteRecord) -> None: ...

    @abstractmethod
    def list_by_tenant(self, tenant_id: str) -> list[InviteRecord]:
        """All invites for a tenant (caller filters by status)."""


class JsonFileInviteStore(InviteStore):
    """Invites in one JSON object {token: record}, filelock-protected."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = FileLock(str(self._path) + ".lock")

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        with self._path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self._path)

    def create(self, invite: InviteRecord) -> None:
        with self._lock:
            data = self._read()
            data[invite.token] = invite.to_dict()
            self._write(data)

    def get(self, token: str) -> InviteRecord | None:
        with self._lock:
            d = self._read().get(token)
        return InviteRecord.from_dict(d) if d else None

    def update(self, invite: InviteRecord) -> None:
        with self._lock:
            data = self._read()
            data[invite.token] = invite.to_dict()
            self._write(data)

    def list_by_tenant(self, tenant_id: str) -> list[InviteRecord]:
        with self._lock:
            return [
                InviteRecord.from_dict(d)
                for d in self._read().values()
                if d.get("tenant_id") == tenant_id
            ]
