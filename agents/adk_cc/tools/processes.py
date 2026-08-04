"""Agent-facing tools for long-running background processes (#108).

Three, deliberately: list, read log, stop. `run_bash(background=True)` is the
fourth verb and lives with bash, because the model already reaches for
run_bash and a rival `start_process` tool would split the intent.

`list_processes` matters more than it looks: without it the model has no way
to know a dev server is ALREADY running and will happily start a second one
on a port that is taken.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .base import AdkCcTool, ToolMeta


class ListProcessesArgs(BaseModel):
    all_projects: bool = Field(
        default=False,
        description="Include processes from other projects (rarely useful).",
    )


class ReadProcessLogArgs(BaseModel):
    process_id: str = Field(description="Process id from run_bash/list_processes.")
    tail_lines: int = Field(
        default=100, description="How many trailing lines to return.")


class StopProcessArgs(BaseModel):
    process_id: str = Field(description="Process id to terminate.")


def _project_id(ctx) -> str:
    try:
        return str(ctx._invocation_context.session.user_id or "")
    except Exception:  # noqa: BLE001
        return ""


class ListProcessesTool(AdkCcTool):
    """What is running right now, so the model does not start a second one."""

    meta = ToolMeta(
        name="list_processes",
        is_read_only=True,
        is_concurrency_safe=True,
        is_destructive=False,
        needs_sandbox=False,
    )
    input_model = ListProcessesArgs
    description = (
        "List long-running background processes (dev servers, watchers) this "
        "project has started, with status, uptime and detected port. Check "
        "this BEFORE starting a server — one may already be running."
    )

    async def _execute(self, args: ListProcessesArgs, ctx) -> dict[str, Any]:
        from ..sandbox.process_registry import get_registry

        reg = get_registry()
        rows = reg.list(project_id=None if args.all_projects else _project_id(ctx))
        return {
            "processes": [
                {
                    "id": r.id, "label": r.label, "status": r.status,
                    "command": r.command, "port": r.port,
                    "elapsed_s": round(
                        (r.finished_at or __import__("time").time())
                        - r.started_at, 1),
                    "exit_code": r.exit_code,
                }
                for r in rows[:40]
            ],
            "running": sum(1 for r in rows if r.status in ("running", "starting")),
        }


class ReadProcessLogTool(AdkCcTool):
    """The log a background process has written so far."""

    meta = ToolMeta(
        name="read_process_log",
        is_read_only=True,
        is_concurrency_safe=True,
        is_destructive=False,
        needs_sandbox=False,
    )
    input_model = ReadProcessLogArgs
    description = (
        "Read the tail of a background process's output log — how you check "
        "whether a server actually came up, or why it died."
    )

    async def _execute(self, args: ReadProcessLogArgs, ctx) -> dict[str, Any]:
        from ..sandbox.process_registry import get_registry

        reg = get_registry()
        rec = reg.get(args.process_id)
        if rec is None:
            return {"error": f"no such process: {args.process_id}"}
        text = reg.read_log(args.process_id)
        lines = text.splitlines()
        tail = lines[-max(1, args.tail_lines):]
        return {
            "id": rec.id, "label": rec.label, "status": rec.status,
            "exit_code": rec.exit_code, "port": rec.port,
            "log": "\n".join(tail),
            "truncated": len(lines) > len(tail),
        }


class StopProcessTool(AdkCcTool):
    """Terminate a background process (group TERM → grace → KILL)."""

    meta = ToolMeta(
        name="stop_process",
        is_read_only=False,
        is_concurrency_safe=False,
        is_destructive=True,
        needs_sandbox=False,
    )
    input_model = StopProcessArgs
    description = (
        "Stop a background process started with run_bash(background=True). "
        "Terminates the whole process group, so a server that forked children "
        "goes with it."
    )

    async def _execute(self, args: StopProcessArgs, ctx) -> dict[str, Any]:
        from ..sandbox.process_registry import get_registry

        reg = get_registry()
        rec = reg.get(args.process_id)
        if rec is None:
            return {"error": f"no such process: {args.process_id}"}
        if not rec.can_terminate:
            return {
                "error": (
                    f"the {rec.backend} backend cannot stop a remote process; "
                    "stop it on the host instead"),
                "id": rec.id,
            }
        if rec.status not in ("running", "starting"):
            return {"id": rec.id, "status": rec.status,
                    "note": "already finished"}
        reg.terminate(args.process_id)
        after = reg.get(args.process_id)
        return {"id": rec.id, "label": rec.label,
                "status": after.status if after else "killed",
                "stopped": True}
