from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..sandbox import SandboxViolation, get_backend, get_workspace
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
        p = resolve(args.path, ctx)
        backend = get_backend(ctx)
        ws = get_workspace(ctx)
        try:
            await backend.write_text(str(p), args.content, fs_write=ws.fs_write_config())
        except SandboxViolation as e:
            return {"status": "sandbox_denied", "error": str(e)}
        return {
            "status": "ok",
            "path": str(p),
            "bytes": len(args.content.encode("utf-8")),
        }
