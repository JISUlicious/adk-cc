"""Task runner — pure tracking, no execution.

After the Stage F refactor: tasks are tracking records; this class is
a thin facade over `TaskStorage`. It exists to keep the existing
`get_runner()` callsites working and to provide a single point where
ID generation lives. Background-execution semantics (asyncio worker,
sandbox exec, FAILED/STOPPED status transitions) have been removed —
the model uses `run_bash` directly when it wants to run a command.
"""

from __future__ import annotations

import uuid
from typing import Optional

from .model import Task
from .storage import JsonFileTaskStorage, TaskStorage


class TaskRunner:
    def __init__(self, *, storage: Optional[TaskStorage] = None) -> None:
        self.storage: TaskStorage = storage or JsonFileTaskStorage()

    async def enqueue(
        self,
        *,
        title: str,
        description: str = "",
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
            blocks=list(blocks or []),
            blocked_by=list(blocked_by or []),
        )
        await self.storage.create(task)
        return task


# Module-level singleton for the dev path. Stage G's tenancy plugin
# replaces this per-tenant.
_default_runner: Optional[TaskRunner] = None


def get_runner() -> TaskRunner:
    global _default_runner
    if _default_runner is None:
        _default_runner = TaskRunner()
    return _default_runner
