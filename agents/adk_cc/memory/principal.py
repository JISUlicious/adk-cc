"""Current-principal contextvar — the bridge that lets the shared compaction
summarizer reach the per-user memory store.

ADK's summarizer is constructed once and only receives `events`, not the
session/state, so it can't resolve who the turn belongs to. MemoryPlugin already
resolves `(tenant_id, user_id)` every `before_model`; it stamps it here, and the
summarizer reads it when seeding the compaction summary with recalled memory
(master-plan P3). Safe because a session is single-user — the last principal seen
in the task IS the session's user. Best-effort: unset → seeding is skipped.
"""

from __future__ import annotations

import contextvars
from typing import Optional

_current: contextvars.ContextVar[Optional[tuple[str, str]]] = contextvars.ContextVar(
    "adk_cc_current_principal", default=None
)


def set_principal(tenant_id: str, user_id: str) -> None:
    _current.set((tenant_id, user_id))


def get_principal() -> Optional[tuple[str, str]]:
    return _current.get()
