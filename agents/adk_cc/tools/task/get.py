from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ...sandbox import get_workspace
from ...tasks import TaskNotFound, get_runner
from ..base import AdkCcTool, ToolMeta
from ..schemas import TaskGetArgs
from ._common import task_not_found_error


class TaskGetTool(AdkCcTool):
    meta = ToolMeta(
        name="task_get",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = TaskGetArgs
    description = "Look up a single task by id. Returns title, status, description, and timestamps."

    async def _execute(self, args: TaskGetArgs, ctx: ToolContext) -> dict[str, Any]:
        runner = get_runner()
        ws = get_workspace(ctx)
        try:
            task = await runner.storage.get(
                args.task_id, tenant_id=ws.tenant_id, workspace_path=ws.abs_path,
            )
        except TaskNotFound:
            return await task_not_found_error(args.task_id, runner, ws)
        return {"status": "ok", "task": task.model_dump(mode="json")}
