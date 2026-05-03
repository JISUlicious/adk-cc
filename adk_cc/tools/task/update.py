from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from google.adk.tools.tool_context import ToolContext

from ...sandbox import get_workspace
from ...tasks import TaskNotFound, TaskStatus, get_runner
from ..base import AdkCcTool, ToolMeta
from ..schemas import TaskUpdateArgs


class TaskUpdateTool(AdkCcTool):
    meta = ToolMeta(
        name="task_update",
        is_read_only=False,
        is_concurrency_safe=False,
    )
    input_model = TaskUpdateArgs
    description = (
        "Update a task's status or description. Set status to "
        "'in_progress' before starting the step and 'completed' "
        "immediately after finishing it; aim for one task `in_progress` "
        "at a time."
    )

    async def _execute(self, args: TaskUpdateArgs, ctx: ToolContext) -> dict[str, Any]:
        runner = get_runner()
        ws = get_workspace(ctx)
        try:
            task = await runner.storage.get(args.task_id, tenant_id=ws.tenant_id)
        except TaskNotFound as e:
            return {"status": "not_found", "error": str(e)}

        if args.status is not None:
            try:
                task.status = TaskStatus(args.status)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"unknown status {args.status!r}; valid: "
                    f"{[s.value for s in TaskStatus]}",
                }
        if args.description is not None:
            task.description = args.description

        task.updated_at = datetime.now(timezone.utc)
        await runner.storage.update(task)
        return {"status": "ok", "task": task.model_dump(mode="json")}
