from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ._fs import resolve
from .base import AdkCcTool, ToolMeta
from .schemas import GlobFilesArgs


class GlobFilesTool(AdkCcTool):
    meta = ToolMeta(
        name="glob_files",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = GlobFilesArgs
    description = "Find files matching a glob pattern under root."

    async def _execute(self, args: GlobFilesArgs, ctx: ToolContext) -> dict[str, Any]:
        base = resolve(args.root)
        if not base.is_dir():
            return {"status": "error", "error": f"not a directory: {base}"}
        matches = [str(p) for p in base.glob(args.pattern) if p.is_file()]
        matches.sort()
        return {
            "status": "ok",
            "matches": matches[:200],
            "truncated": len(matches) > 200,
        }
