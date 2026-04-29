from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ...sandbox import get_workspace
from ...tasks import TaskNotFound, get_runner
from ..base import AdkCcTool, ToolMeta
from ..schemas import TaskStopArgs


class TaskStopTool(AdkCcTool):
    meta = ToolMeta(
        name="task_stop",
        is_read_only=False,
        is_concurrency_safe=False,
        is_destructive=True,
    )
    input_model = TaskStopArgs
    description = "Cancel a running background task. No-op if already terminal."

    async def _execute(self, args: TaskStopArgs, ctx: ToolContext) -> dict[str, Any]:
        runner = get_runner()
        ws = get_workspace(ctx)
        try:
            task = await runner.stop(args.task_id, tenant_id=ws.tenant_id)
        except TaskNotFound as e:
            return {"status": "not_found", "error": str(e)}
        return {"status": "ok", "task": task.model_dump(mode="json")}
