from __future__ import annotations

import re
from typing import Any

from google.adk.tools.tool_context import ToolContext

from ._fs import resolve
from .base import AdkCcTool, ToolMeta
from .schemas import GrepArgs


class GrepTool(AdkCcTool):
    meta = ToolMeta(
        name="grep",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = GrepArgs
    description = "Search for a regex pattern across files under path."

    async def _execute(self, args: GrepArgs, ctx: ToolContext) -> dict[str, Any]:
        base = resolve(args.path)
        try:
            rx = re.compile(args.pattern)
        except re.error as e:
            return {"status": "error", "error": f"bad regex: {e}"}
        hits: list[dict] = []
        for p in base.glob(args.glob):
            if not p.is_file():
                continue
            try:
                for i, line in enumerate(
                    p.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if rx.search(line):
                        hits.append({"file": str(p), "line": i, "text": line[:300]})
                        if len(hits) >= 200:
                            return {"status": "ok", "hits": hits, "truncated": True}
            except (UnicodeDecodeError, OSError):
                continue
        return {"status": "ok", "hits": hits, "truncated": False}
