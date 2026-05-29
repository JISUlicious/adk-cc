"""Read the current session's plan from state, with history visibility.

Companion to `write_plan`. Looks up `state["current_plan_path"]` for the
latest plan and `state["plan_history"]` for the full session history,
then reads and returns the latest plan's contents. Useful for:

  - The coordinator resuming work against a plan it wrote earlier in
    the session (e.g. across plan-mode → execute transitions).
  - Verification reading the success criteria the plan committed to.
  - The coordinator refining an existing plan thread on re-entry to
    plan mode.

Returns `status: no_plan` when nothing has been written yet. The
`history` field is always present (empty list when no plans exist).
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..sandbox import SandboxViolation, get_backend, get_workspace
from .base import AdkCcTool, ToolMeta
from .schemas import ReadCurrentPlanArgs


class ReadCurrentPlanTool(AdkCcTool):
    meta = ToolMeta(
        name="read_current_plan",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = ReadCurrentPlanArgs
    description = (
        "Read the current session's plan, written by `write_plan`. "
        "Returns the plan's path, title, and full Markdown content. "
        "Returns status='no_plan' if none has been written yet."
    )

    async def _execute(
        self, args: ReadCurrentPlanArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        try:
            path = ctx.state.get("current_plan_path")
            title = ctx.state.get("current_plan_title")
            history = ctx.state.get("plan_history") or []
        except Exception:
            path, title, history = None, None, []

        # Strip content from history entries — keep payloads small; the
        # caller can read older plans via read_file with the listed paths.
        history_summary = [
            {k: v for k, v in entry.items() if k != "content"}
            for entry in history
        ]

        if not path:
            return {"status": "no_plan", "history": history_summary}

        ws = get_workspace(ctx)
        backend = get_backend(ctx)
        try:
            content = await backend.read_text(path, fs_read=ws.fs_read_config())
        except SandboxViolation as e:
            return {"status": "sandbox_denied", "error": str(e), "history": history_summary}
        except FileNotFoundError:
            return {
                "status": "no_plan",
                "history": history_summary,
                "warning": (
                    f"current_plan_path was set to {path!r} but the file no "
                    "longer exists; enter plan mode and call `write_plan` "
                    "to produce a fresh one."
                ),
            }

        return {
            "status": "ok",
            "path": path,
            "title": title,
            "content": content,
            "history": history_summary,
        }
