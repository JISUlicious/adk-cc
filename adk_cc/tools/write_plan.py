"""Persist the Plan sub-agent's plan as an artifact.

Mirrors upstream Claude Code's plan-file pattern (`utils/plans.ts`):
the plan lives on disk, its path is recorded in session state, and
other agents (coordinator, verification, future iterations of Plan
itself) discover the file via state and read it as needed.

Path layout:
    <workspace>/.adk-cc/plan.md          # single canonical plan per session

State keys written:
    current_plan_path:    absolute path to the plan file
    current_plan_title:   first `# heading` from the content (display label)

Why this tool is `is_read_only=True` even though it writes a file:
the artifact is part of the planning role, not a project mutation. The
file still goes through the sandbox backend's `write_text`, so the
workspace fs_write_config governs where it can land (always under the
workspace root, never outside).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..sandbox import SandboxViolation, get_backend, get_workspace
from .base import AdkCcTool, ToolMeta
from .schemas import WritePlanArgs

_PLAN_FILE = ".adk-cc/plan.md"


def _extract_title(content: str) -> str:
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled plan"


class WritePlanTool(AdkCcTool):
    meta = ToolMeta(
        name="write_plan",
        is_read_only=True,  # planning artifact; see module docstring
        is_concurrency_safe=False,
    )
    input_model = WritePlanArgs
    description = (
        "Write or replace the current session's plan. Saves a Markdown "
        "file under the workspace and records its path in session state "
        "as `current_plan_path` so other agents can read it via "
        "`read_current_plan`. Plan agents must call this with their final "
        "plan; do not return the plan as text only."
    )

    async def _execute(self, args: WritePlanArgs, ctx: ToolContext) -> dict[str, Any]:
        ws = get_workspace(ctx)
        backend = get_backend(ctx)
        plan_path = str(Path(ws.abs_path) / _PLAN_FILE)
        try:
            await backend.write_text(
                plan_path, args.content, fs_write=ws.fs_write_config()
            )
        except SandboxViolation as e:
            return {"status": "sandbox_denied", "error": str(e)}

        title = _extract_title(args.content)
        try:
            ctx.state["current_plan_path"] = plan_path
            ctx.state["current_plan_title"] = title
        except Exception:
            pass

        return {
            "status": "ok",
            "path": plan_path,
            "title": title,
            "bytes": len(args.content.encode("utf-8")),
        }
