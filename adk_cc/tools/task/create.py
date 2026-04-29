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
        # `command` jobs run via the sandbox; checkpoint-only tasks don't.
        # Mark destructive so the permission engine asks in DEFAULT mode.
        is_destructive=True,
        long_running=True,
    )
    input_model = TaskCreateArgs
    description = (
        "Create a background task or checkpoint. With `command`, the task "
        "runs in the sandbox and you can poll via task_get. Without "
        "`command`, the task is a checkpoint you update manually via "
        "task_update."
    )

    async def _execute(
        self, args: TaskCreateArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        runner = get_runner()
        ws = get_workspace(ctx)
        task = await runner.enqueue(
            title=args.title,
            description=args.description,
            command=args.command,
            tenant_id=ws.tenant_id,
            session_id=ws.session_id,
            blocked_by=args.blocked_by,
        )
        return {
            "status": "created",
            "task_id": task.id,
            "task_status": task.status.value,
            "is_background": args.command is not None,
        }
