"""Task storage.

Two implementations:

  - `JsonFileTaskStorage` — default. One JSON file per task at
    `<root>/<tenant_id>/<session_id>/<task_id>.json`. Survives process
    restarts; `filelock` serializes writes for multi-worker uvicorn
    deployments. Mirrors upstream Claude Code's per-task JSON layout
    (`src/utils/tasks.ts`).
  - `InMemoryTaskStorage` — kept for tests and ephemeral use; lost on
    restart.

Operators with stricter durability requirements (Postgres, Redis) can
implement the `TaskStorage` ABC themselves.
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from filelock import FileLock, Timeout

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


def _default_root() -> Path:
    raw = os.environ.get("ADK_CC_TASKS_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".adk-cc" / "tasks"


class JsonFileTaskStorage(TaskStorage):
    """Persists tasks as one JSON file per task.

    Layout:
        <root>/<tenant_id>/<session_id>/<task_id>.json
        <root>/<tenant_id>/<session_id>/.lock     (filelock)

    `<root>` defaults to `~/.adk-cc/tasks/` (override via
    `ADK_CC_TASKS_DIR`). Mirrors upstream Claude Code's per-task JSON
    layout (`src/utils/tasks.ts:229`).

    File operations run under `asyncio.to_thread` so they don't block
    the event loop, and the on-disk write is wrapped in a
    `filelock.FileLock` (5s timeout) to serialize concurrent writers
    in multi-worker deployments.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root or _default_root()

    def _list_dir(self, tenant_id: str, session_id: str) -> Path:
        return self._root / tenant_id / session_id

    def _task_path(self, t: Task) -> Path:
        return self._list_dir(t.tenant_id, t.session_id) / f"{t.id}.json"

    def _lock_path(self, tenant_id: str, session_id: str) -> Path:
        return self._list_dir(tenant_id, session_id) / ".lock"

    def _write_atomic(
        self, *, tenant_id: str, session_id: str, path: Path, data: str
    ) -> None:
        list_dir = self._list_dir(tenant_id, session_id)
        list_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_path(tenant_id, session_id)
        try:
            with FileLock(str(lock_file), timeout=5):
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(data, encoding="utf-8")
                tmp.replace(path)
        except Timeout as e:
            raise RuntimeError(
                f"task storage lock contended at {lock_file}: {e}"
            ) from e

    async def create(self, task: Task) -> Task:
        await asyncio.to_thread(
            self._write_atomic,
            tenant_id=task.tenant_id,
            session_id=task.session_id,
            path=self._task_path(task),
            data=task.model_dump_json(indent=2),
        )
        return task

    async def get(self, task_id: str, *, tenant_id: str) -> Task:
        # We don't know the session_id from the args; walk the tenant's
        # session subdirs. Typical sessions hold few tasks so this is cheap.
        def _read() -> Optional[Task]:
            tenant_dir = self._root / tenant_id
            if not tenant_dir.is_dir():
                return None
            for session_dir in tenant_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                p = session_dir / f"{task_id}.json"
                if p.is_file():
                    return Task.model_validate_json(p.read_text(encoding="utf-8"))
            return None

        t = await asyncio.to_thread(_read)
        if t is None:
            raise TaskNotFound(
                f"task {task_id!r} not found for tenant {tenant_id!r}"
            )
        return t

    async def update(self, task: Task) -> Task:
        path = self._task_path(task)
        if not await asyncio.to_thread(path.is_file):
            raise TaskNotFound(f"task {task.id!r} not found")
        await asyncio.to_thread(
            self._write_atomic,
            tenant_id=task.tenant_id,
            session_id=task.session_id,
            path=path,
            data=task.model_dump_json(indent=2),
        )
        return task

    async def list(
        self,
        *,
        tenant_id: str,
        session_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
    ) -> list[Task]:
        def _scan() -> list[Task]:
            tenant_dir = self._root / tenant_id
            if not tenant_dir.is_dir():
                return []
            session_dirs: list[Path]
            if session_id is not None:
                d = tenant_dir / session_id
                session_dirs = [d] if d.is_dir() else []
            else:
                session_dirs = [d for d in tenant_dir.iterdir() if d.is_dir()]
            out: list[Task] = []
            for sd in session_dirs:
                for jf in sd.glob("*.json"):
                    if jf.name.startswith("."):
                        continue
                    try:
                        t = Task.model_validate_json(jf.read_text(encoding="utf-8"))
                    except Exception:
                        # Skip malformed files rather than failing the list.
                        continue
                    if status is None or t.status == status:
                        out.append(t)
            out.sort(key=lambda t: t.created_at)
            return out

        return await asyncio.to_thread(_scan)
