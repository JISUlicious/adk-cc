from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ._fs import resolve
from .base import AdkCcTool, ToolMeta
from .schemas import WriteFileArgs


class WriteFileTool(AdkCcTool):
    meta = ToolMeta(
        name="write_file",
        is_read_only=False,
        is_concurrency_safe=False,
        is_destructive=True,
        needs_sandbox=True,
    )
    input_model = WriteFileArgs
    description = "Write text to a file, creating parent directories if needed."

    async def _execute(self, args: WriteFileArgs, ctx: ToolContext) -> dict[str, Any]:
        p = resolve(args.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args.content, encoding="utf-8")
        return {
            "status": "ok",
            "path": str(p),
            "bytes": len(args.content.encode("utf-8")),
        }
