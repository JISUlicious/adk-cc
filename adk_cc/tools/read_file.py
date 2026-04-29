from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..sandbox import SandboxViolation, get_backend, get_workspace
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
        p = resolve(args.path, ctx)
        backend = get_backend(ctx)
        ws = get_workspace(ctx)
        try:
            text = await backend.read_text(str(p), fs_read=ws.fs_read_config())
        except SandboxViolation as e:
            return {"status": "sandbox_denied", "error": str(e)}
        except FileNotFoundError:
            return {"status": "error", "error": f"file not found: {p}"}
        except IsADirectoryError:
            return {"status": "error", "error": f"not a regular file: {p}"}
        except UnicodeDecodeError:
            return {"status": "error", "error": f"non-utf8 file: {p}"}
        return {"status": "ok", "path": str(p), "content": text}
