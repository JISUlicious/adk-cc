"""Task data model.

Two task shapes share one schema:
  - **Background job** (`command` set): the worker runs it via the active
    SandboxBackend.exec and records stdout/stderr/exit_code in `output`.
  - **Checkpoint / todo item** (`command` unset): the agent updates
    `status` manually as it works through the plan.

Dependencies via `blocks` / `blocked_by` aren't enforced by the runner
in v1 — they're metadata for the agent to read. Promotion to a real
scheduler (don't run a task while its blockers are pending) is a future
extension; the schema is forward-compatible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Task(BaseModel):
    id: str
    tenant_id: str = "local"
    session_id: str = "local"
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    command: Optional[str] = Field(
        default=None,
        description=(
            "If set, the worker runs this via SandboxBackend.exec. "
            "If unset, the task is a passive checkpoint with manual status updates."
        ),
    )
    blocks: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    output: Optional[dict[str, Any]] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
