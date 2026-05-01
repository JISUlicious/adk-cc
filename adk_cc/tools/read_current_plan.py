"""Read the current session's plan file from session state.

Companion to `write_plan`. Looks up `state["current_plan_path"]` and
returns the file's contents along with the recorded title. Useful for:

  - Coordinator picking up where Plan sub-agent left off
  - Verification reading the success criteria the plan committed to
  - A second invocation of Plan refining the previous plan

Returns `status: no_plan` when no plan exists yet.
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
        except Exception:
            path, title = None, None

        if not path:
            return {"status": "no_plan"}

        ws = get_workspace(ctx)
        backend = get_backend(ctx)
        try:
            content = await backend.read_text(path, fs_read=ws.fs_read_config())
        except SandboxViolation as e:
            return {"status": "sandbox_denied", "error": str(e)}
        except FileNotFoundError:
            return {
                "status": "no_plan",
                "warning": (
                    f"current_plan_path was set to {path!r} but the file no "
                    "longer exists; ask Plan to write a fresh plan."
                ),
            }

        return {
            "status": "ok",
            "path": path,
            "title": title,
            "content": content,
        }
