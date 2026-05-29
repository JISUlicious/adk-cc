"""Task data model.

Pure tracking record — mirrors upstream Claude Code v2's Task schema
(`src/utils/tasks.ts:76-89`). The model only carries enough fields to
represent "what work exists and where it stands"; execution lives
elsewhere (the model uses `run_bash` directly when it wants to run
something).

Dependencies via `blocks` / `blocked_by` aren't enforced — they're
metadata for the agent to read. The agent decides ordering; the task
record just remembers the relationships.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Task(BaseModel):
    id: str
    tenant_id: str = "local"
    session_id: str = "local"
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    blocks: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
