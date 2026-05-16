"""RenderTableTool — markdown-style table for the user-facing reply."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import stash_result


class _Args(BaseModel):
    name: str = Field(..., description="Dataset name.")
    max_rows: int = Field(10, ge=1, le=50, description="Row cap.")


class RenderTableTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="render_table",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Render a dataset as a markdown-style table (up to max_rows). "
        "Use for the final tabular summary."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)[: args.max_rows]
        if not rows:
            return {"status": "ok", "name": args.name, "table": ""}
        cols = list(rows[0].keys())
        header = " | ".join(cols)
        sep = " | ".join(["---"] * len(cols))
        body = "\n".join(" | ".join(str(r.get(c, "")) for c in cols) for r in rows)
        table = f"{header}\n{sep}\n{body}"
        result = {
            "status": "ok",
            "name": args.name,
            "rows_rendered": len(rows),
            "table": table,
        }
        stash_result(ctx, "render_table", args.model_dump(), result)
        return result
