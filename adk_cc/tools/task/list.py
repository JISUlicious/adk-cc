from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ...sandbox import get_workspace
from ...tasks import TaskStatus, get_runner
from ..base import AdkCcTool, ToolMeta
from ..schemas import TaskListArgs


class TaskListTool(AdkCcTool):
    meta = ToolMeta(
        name="task_list",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = TaskListArgs
    description = (
        "List tasks for the current session. Optional `status` filter "
        "(pending|in_progress|completed|failed|stopped)."
    )

    async def _execute(self, args: TaskListArgs, ctx: ToolContext) -> dict[str, Any]:
        runner = get_runner()
        ws = get_workspace(ctx)

        status_filter = None
        if args.status is not None:
            try:
                status_filter = TaskStatus(args.status)
            except ValueError:
                return {
                    "status": "error",
                    "error": f"unknown status {args.status!r}; valid: "
                    f"{[s.value for s in TaskStatus]}",
                }

        tasks = await runner.storage.list(
            tenant_id=ws.tenant_id,
            session_id=ws.session_id,
            status=status_filter,
        )
        return {
            "status": "ok",
            "tasks": [t.model_dump(mode="json") for t in tasks],
            "count": len(tasks),
        }
