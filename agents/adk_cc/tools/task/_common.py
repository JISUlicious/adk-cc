from __future__ import annotations

from typing import Any


async def task_not_found_error(task_id: str, runner: Any, ws: Any) -> dict[str, Any]:
    """Actionable not-found response that lists the session's real task ids.

    A bare "task X not found for tenant Y" reads to the model like the
    whole session is empty — it then abandons tracking. Including the
    current ids turns it into a recoverable "you used the wrong id, here
    are the right ones" so the model retries instead of giving up.
    """
    try:
        existing = await runner.storage.list(
            tenant_id=ws.tenant_id,
            session_id=ws.session_id,
            workspace_path=ws.abs_path,
        )
    except Exception:
        existing = []
    listing = [
        {
            "task_id": t.id,
            "status": t.status.value if hasattr(t.status, "value") else t.status,
            "title": t.title,
        }
        for t in existing
    ]
    if listing:
        msg = (
            f"No task with id {task_id!r} exists in this session. The "
            f"session is NOT empty — use one of the existing task ids in "
            f"`existing_tasks` below (copy it exactly), or call task_create "
            f"to add a new task. Do not assume tracking was lost."
        )
    else:
        msg = (
            f"No task with id {task_id!r} exists, and this session has no "
            f"tasks yet. Call task_create first, then use the id it returns."
        )
    return {"status": "not_found", "error": msg, "existing_tasks": listing}
