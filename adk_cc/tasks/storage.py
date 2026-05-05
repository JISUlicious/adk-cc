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

Path resolution (JsonFileTaskStorage):
  - When `workspace_path` is passed to a method AND `ADK_CC_TASKS_DIR`
    is unset, tasks live at `<workspace_path>/.adk-cc/tasks/<session>/`.
    This is the production layout — tasks travel with the user's
    workspace, get backed up alongside their files, get wiped by
    tenant offboarding.
  - When `workspace_path` is unset OR `ADK_CC_TASKS_DIR` is set, tasks
    live at `<root>/<tenant>/<session>/` (legacy). Dev path uses this
    when called without workspace_path; central-storage operators set
    `ADK_CC_TASKS_DIR` to keep all tenants' tasks under one root.
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
    """Pure tracking persistence.

    All methods accept an optional `workspace_path` so the JsonFile impl
    can anchor task files in the user's workspace (production layout).
    Backends that don't care about path layout (InMemory, future SQL)
    ignore the parameter.
    """

    @abstractmethod
    async def create(
        self, task: Task, *, workspace_path: Optional[str] = None
    ) -> Task: ...

    @abstractmethod
    async def get(
        self,
        task_id: str,
        *,
        tenant_id: str,
        workspace_path: Optional[str] = None,
    ) -> Task: ...

    @abstractmethod
    async def update(
        self, task: Task, *, workspace_path: Optional[str] = None
    ) -> Task: ...

    @abstractmethod
    async def list(
        self,
        *,
        tenant_id: str,
        session_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
        workspace_path: Optional[str] = None,
    ) -> list[Task]: ...


class InMemoryTaskStorage(TaskStorage):
    def __init__(self) -> None:
        self._tasks: dict[tuple[str, str], Task] = {}
        self._lock = asyncio.Lock()

    async def create(
        self, task: Task, *, workspace_path: Optional[str] = None
    ) -> Task:
        async with self._lock:
            self._tasks[(task.tenant_id, task.id)] = task
            return task

    async def get(
        self,
        task_id: str,
        *,
        tenant_id: str,
        workspace_path: Optional[str] = None,
    ) -> Task:
        async with self._lock:
            t = self._tasks.get((tenant_id, task_id))
            if t is None:
                raise TaskNotFound(f"task {task_id!r} not found for tenant {tenant_id!r}")
            return t

    async def update(
        self, task: Task, *, workspace_path: Optional[str] = None
    ) -> Task:
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
        workspace_path: Optional[str] = None,
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
        # Legacy root — used when workspace_path is unset OR
        # ADK_CC_TASKS_DIR is set (operator opted into central storage).
        self._root = root or _default_root()
        # When ADK_CC_TASKS_DIR is set explicitly, we always use legacy
        # path resolution regardless of workspace_path. Operators
        # wanting central task storage win over the workspace-anchored
        # default.
        self._tasks_dir_override = bool(os.environ.get("ADK_CC_TASKS_DIR"))

    def _list_dir(
        self,
        tenant_id: str,
        session_id: str,
        workspace_path: Optional[str] = None,
    ) -> Path:
        """Resolve the tasks dir for a (tenant, session) tuple.

        Production layout: when `workspace_path` is set (caller is in a
        TenancyPlugin-seeded session) AND ADK_CC_TASKS_DIR is unset,
        anchor at `<workspace>/.adk-cc/tasks/<session>/`. Tasks travel
        with the user's workspace.

        Legacy: otherwise `<root>/<tenant>/<session>/`. Used by:
          - dev calls without workspace_path
          - operators who set ADK_CC_TASKS_DIR for central storage
          - existing callers / tests that don't pass workspace_path
        """
        if workspace_path and not self._tasks_dir_override:
            return Path(workspace_path) / ".adk-cc" / "tasks" / session_id
        return self._root / tenant_id / session_id

    def _task_path(
        self, t: Task, workspace_path: Optional[str] = None
    ) -> Path:
        return (
            self._list_dir(t.tenant_id, t.session_id, workspace_path)
            / f"{t.id}.json"
        )

    def _lock_path(
        self,
        tenant_id: str,
        session_id: str,
        workspace_path: Optional[str] = None,
    ) -> Path:
        return self._list_dir(tenant_id, session_id, workspace_path) / ".lock"

    def _write_atomic(
        self,
        *,
        tenant_id: str,
        session_id: str,
        path: Path,
        data: str,
        workspace_path: Optional[str] = None,
    ) -> None:
        list_dir = self._list_dir(tenant_id, session_id, workspace_path)
        list_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_path(tenant_id, session_id, workspace_path)
        try:
            with FileLock(str(lock_file), timeout=5):
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(data, encoding="utf-8")
                tmp.replace(path)
        except Timeout as e:
            raise RuntimeError(
                f"task storage lock contended at {lock_file}: {e}"
            ) from e

    async def create(
        self, task: Task, *, workspace_path: Optional[str] = None
    ) -> Task:
        await asyncio.to_thread(
            self._write_atomic,
            tenant_id=task.tenant_id,
            session_id=task.session_id,
            path=self._task_path(task, workspace_path),
            data=task.model_dump_json(indent=2),
            workspace_path=workspace_path,
        )
        return task

    async def get(
        self,
        task_id: str,
        *,
        tenant_id: str,
        workspace_path: Optional[str] = None,
    ) -> Task:
        # We don't know the session_id from the args; walk the per-session
        # subdirs of the relevant root. Typical sessions hold few tasks so
        # this is cheap.
        def _read() -> Optional[Task]:
            if workspace_path and not self._tasks_dir_override:
                # Production: search under <workspace>/.adk-cc/tasks/.
                tenant_dir = Path(workspace_path) / ".adk-cc" / "tasks"
            else:
                # Legacy: search under <root>/<tenant>/.
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

    async def update(
        self, task: Task, *, workspace_path: Optional[str] = None
    ) -> Task:
        path = self._task_path(task, workspace_path)
        if not await asyncio.to_thread(path.is_file):
            raise TaskNotFound(f"task {task.id!r} not found")
        await asyncio.to_thread(
            self._write_atomic,
            tenant_id=task.tenant_id,
            session_id=task.session_id,
            path=path,
            data=task.model_dump_json(indent=2),
            workspace_path=workspace_path,
        )
        return task

    async def list(
        self,
        *,
        tenant_id: str,
        session_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
        workspace_path: Optional[str] = None,
    ) -> list[Task]:
        def _scan() -> list[Task]:
            if workspace_path and not self._tasks_dir_override:
                tenant_dir = Path(workspace_path) / ".adk-cc" / "tasks"
            else:
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
