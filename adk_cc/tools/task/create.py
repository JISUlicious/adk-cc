from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ...sandbox import get_workspace
from ...tasks import get_runner
from ..base import AdkCcTool, ToolMeta
from ..schemas import TaskCreateArgs


class TaskCreateTool(AdkCcTool):
    meta = ToolMeta(
        name="task_create",
        is_read_only=False,
        is_concurrency_safe=False,
    )
    input_model = TaskCreateArgs
    description = (
        "Create a tracking task with status 'pending'. Use to track a "
        "step in a multi-step plan; transition the status with task_update "
        "as you work. Tasks persist as JSON files under the workspace and "
        "survive across the coordinator's turns."
    )

    async def _execute(
        self, args: TaskCreateArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        runner = get_runner()
        ws = get_workspace(ctx)
        task = await runner.enqueue(
            title=args.title,
            description=args.description,
            tenant_id=ws.tenant_id,
            session_id=ws.session_id,
            blocked_by=args.blocked_by,
        )
        return {
            "status": "created",
            "task_id": task.id,
            "task_status": task.status.value,
        }
