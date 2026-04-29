from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ._fs import resolve
from .base import AdkCcTool, ToolMeta
from .schemas import ReadFileArgs


class ReadFileTool(AdkCcTool):
    meta = ToolMeta(
        name="read_file",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = ReadFileArgs
    description = "Read a UTF-8 text file and return its contents."

    async def _execute(self, args: ReadFileArgs, ctx: ToolContext) -> dict[str, Any]:
        p = resolve(args.path)
        if not p.exists():
            return {"status": "error", "error": f"file not found: {p}"}
        if not p.is_file():
            return {"status": "error", "error": f"not a regular file: {p}"}
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {"status": "error", "error": f"non-utf8 file: {p}"}
        return {"status": "ok", "path": str(p), "content": text}
