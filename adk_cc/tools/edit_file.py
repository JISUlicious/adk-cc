from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..sandbox import SandboxViolation, get_backend, get_workspace
from ._fs import resolve
from .base import AdkCcTool, ToolMeta
from .schemas import EditFileArgs


class EditFileTool(AdkCcTool):
    meta = ToolMeta(
        name="edit_file",
        is_read_only=False,
        is_concurrency_safe=False,
        is_destructive=True,
        needs_sandbox=True,
    )
    input_model = EditFileArgs
    description = (
        "Replace the first occurrence of old_string with new_string in a file. "
        "old_string must be unique in the file."
    )

    async def _execute(self, args: EditFileArgs, ctx: ToolContext) -> dict[str, Any]:
        p = resolve(args.path, ctx)
        backend = get_backend(ctx)
        ws = get_workspace(ctx)
        try:
            text = await backend.read_text(str(p), fs_read=ws.fs_read_config())
        except SandboxViolation as e:
            return {"status": "sandbox_denied", "error": str(e)}
        except FileNotFoundError:
            return {"status": "error", "error": f"file not found: {p}"}
        occurrences = text.count(args.old_string)
        if occurrences == 0:
            return {"status": "error", "error": "old_string not found"}
        if occurrences > 1:
            return {
                "status": "error",
                "error": f"old_string is not unique ({occurrences} matches)",
            }
        new_text = text.replace(args.old_string, args.new_string, 1)
        try:
            await backend.write_text(str(p), new_text, fs_write=ws.fs_write_config())
        except SandboxViolation as e:
            return {"status": "sandbox_denied", "error": str(e)}
        return {"status": "ok", "path": str(p)}
