from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ...sandbox import get_workspace
from ...tasks import get_runner
from ..base import AdkCcTool, ToolMeta
from ..schemas import TaskCreateArgs


class TaskCreateTool(AdkCcTool):
    # NOTE on destructiveness: this tool currently accepts an optional
    # `command` arg; when set, TaskRunner schedules an asyncio worker to
    # run the command via the sandbox backend. That mixes pure tracking
    # with backgrounded execution — a Stage-F-era coupling. Marking the
    # whole tool destructive (forcing a confirmation prompt for every
    # task creation, including plain checklist items) was the conservative
    # workaround. Now that the typical use is checklist-only, treat the
    # tool as non-destructive at the meta level. If `command` is set, the
    # sandbox backend's policy still constrains what the command can do;
    # if operators want a confirmation gate specifically for
    # backgrounded execution, that belongs on a separate
    # background-execution tool, not this tracking tool.
    meta = ToolMeta(
        name="task_create",
        is_read_only=False,
        is_concurrency_safe=False,
        # long_running stays True to signal that ADK may need to wait
        # when `command` is set; it has no effect on tracking-only calls.
        long_running=True,
    )
    input_model = TaskCreateArgs
    description = (
        "Create a tracking item (a 'task'). For checklist-style tracking, "
        "pass title + description; status starts at 'pending', the model "
        "transitions it via task_update. Optional `command` schedules the "
        "task to run in the background sandbox (legacy execution path "
        "from Stage F)."
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
