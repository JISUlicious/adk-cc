"""Task storage.

`TaskStorage` is the operator extension point. v1 ships:
  - `InMemoryTaskStorage` — the default; lost on process restart.

Operators with durability requirements implement `TaskStorage` against
Postgres / SQLite / Redis / etc. The plan calls for Postgres with
row-level locking — straightforward to add via SQLAlchemy:

    class PostgresTaskStorage(TaskStorage):
        def __init__(self, dsn): ...
        async def create(self, task): UPSERT INTO tasks ...
        async def get(self, id, tenant_id): SELECT ... FOR SHARE
        ...
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Optional

from .model import Task, TaskStatus


class TaskNotFound(Exception):
    pass


class TaskStorage(ABC):
    @abstractmethod
    async def create(self, task: Task) -> Task: ...

    @abstractmethod
    async def get(self, task_id: str, *, tenant_id: str) -> Task: ...

    @abstractmethod
    async def update(self, task: Task) -> Task: ...

    @abstractmethod
    async def list(
        self,
        *,
        tenant_id: str,
        session_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
    ) -> list[Task]: ...


class InMemoryTaskStorage(TaskStorage):
    def __init__(self) -> None:
        self._tasks: dict[tuple[str, str], Task] = {}
        self._lock = asyncio.Lock()

    async def create(self, task: Task) -> Task:
        async with self._lock:
            self._tasks[(task.tenant_id, task.id)] = task
            return task

    async def get(self, task_id: str, *, tenant_id: str) -> Task:
        async with self._lock:
            t = self._tasks.get((tenant_id, task_id))
            if t is None:
                raise TaskNotFound(f"task {task_id!r} not found for tenant {tenant_id!r}")
            return t

    async def update(self, task: Task) -> Task:
        async with self._lock:
            key = (task.tenant_id, task.id)
            if key not in self._tasks:
                raise TaskNotFound(f"task {task.id!r} not found")
            self._tasks[key] = task
            return task

    async def list(
        self,
        *,
        tenant_id: str,
        session_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
    ) -> list[Task]:
        async with self._lock:
            out = [
                t for (tid, _), t in self._tasks.items()
                if tid == tenant_id
                and (session_id is None or t.session_id == session_id)
                and (status is None or t.status == status)
            ]
            return sorted(out, key=lambda t: t.created_at)
