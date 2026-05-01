"""Background task runner.

Owns:
  - A `TaskStorage` instance (default: `JsonFileTaskStorage` — tasks
    persist to disk under `~/.adk-cc/tasks/` so they survive restarts).
  - A pool of asyncio Tasks executing jobs whose `command` is set.

Lifecycle:
  - `enqueue(task)`: persist as PENDING, schedule an asyncio.Task.
  - The asyncio.Task transitions storage: PENDING → IN_PROGRESS → COMPLETED/FAILED.
  - `stop(task_id)`: cancels the asyncio.Task and writes STOPPED.

Tasks without a `command` (checkpoints / todos) are persisted but never
scheduled — the agent updates them manually.

Concurrency: the runner runs jobs in parallel by default. To serialize
or limit concurrency, wrap `enqueue` with an `asyncio.Semaphore`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..sandbox import SandboxBackend
from ..sandbox.config import FsWriteConfig, NetworkConfig
from .model import Task, TaskStatus
from .storage import JsonFileTaskStorage, TaskStorage


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TaskRunner:
    def __init__(
        self,
        *,
        storage: Optional[TaskStorage] = None,
        backend: Optional[SandboxBackend] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self.storage: TaskStorage = storage or JsonFileTaskStorage()
        self._backend = backend
        self._cwd = cwd
        self._asyncio_tasks: dict[str, asyncio.Task] = {}

    def _executor_or_none(self) -> Optional[SandboxBackend]:
        # Resolved lazily so the runner can be constructed before the
        # default backend's first use (which initializes a NoopBackend).
        if self._backend is not None:
            return self._backend
        try:
            from ..sandbox import _get_default_backend
            return _get_default_backend()
        except Exception:
            return None

    async def enqueue(
        self,
        *,
        title: str,
        description: str = "",
        command: Optional[str] = None,
        tenant_id: str = "local",
        session_id: str = "local",
        blocks: Optional[list[str]] = None,
        blocked_by: Optional[list[str]] = None,
    ) -> Task:
        task = Task(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            session_id=session_id,
            title=title,
            description=description,
            command=command,
            blocks=list(blocks or []),
            blocked_by=list(blocked_by or []),
        )
        await self.storage.create(task)
        if command is not None:
            self._asyncio_tasks[task.id] = asyncio.create_task(self._run(task))
        return task

    async def _run(self, task: Task) -> None:
        try:
            task.status = TaskStatus.IN_PROGRESS
            task.updated_at = _now()
            await self.storage.update(task)

            backend = self._executor_or_none()
            if backend is None:
                task.status = TaskStatus.FAILED
                task.output = {"error": "no sandbox backend available"}
            else:
                result = await backend.exec(
                    task.command or "",
                    fs_write=FsWriteConfig(),
                    network=NetworkConfig(),
                    timeout_s=300,
                    cwd=self._cwd or ".",
                )
                task.output = {
                    "exit_code": result.exit_code,
                    "stdout": result.stdout[-4000:],
                    "stderr": result.stderr[-2000:],
                    "timed_out": result.timed_out,
                }
                task.status = (
                    TaskStatus.COMPLETED if result.exit_code == 0
                    else TaskStatus.FAILED
                )
        except asyncio.CancelledError:
            task.status = TaskStatus.STOPPED
            raise
        except Exception as e:  # noqa: BLE001
            task.status = TaskStatus.FAILED
            task.output = {"error": f"{type(e).__name__}: {e}"}
        finally:
            task.updated_at = _now()
            await self.storage.update(task)
            self._asyncio_tasks.pop(task.id, None)

    async def stop(self, task_id: str, *, tenant_id: str = "local") -> Task:
        t = await self.storage.get(task_id, tenant_id=tenant_id)
        async_task = self._asyncio_tasks.pop(task_id, None)
        if async_task is not None and not async_task.done():
            async_task.cancel()
        if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
            t.status = TaskStatus.STOPPED
            t.updated_at = _now()
            await self.storage.update(t)
        return t


# Module-level singleton for the dev path. Stage G's tenancy plugin
# replaces this per-tenant.
_default_runner: Optional[TaskRunner] = None


def get_runner() -> TaskRunner:
    global _default_runner
    if _default_runner is None:
        _default_runner = TaskRunner()
    return _default_runner
