"""SelectColumnsTool — project to a subset of columns."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import stash_result


class _Args(BaseModel):
    name: str = Field(..., description="Dataset name.")
    columns: list[str] = Field(..., min_length=1, description="Columns to keep.")


class SelectColumnsTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="select_columns",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Project a dataset down to a subset of columns. Returns the "
        "narrowed rows; does not mutate the registry."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)
        missing = [c for c in args.columns if c not in (rows[0] if rows else {})]
        if missing:
            return {"status": "error", "error": f"unknown columns: {missing}"}
        out = [{c: r[c] for c in args.columns} for r in rows]
        result = {
            "status": "ok",
            "name": args.name,
            "columns": args.columns,
            "rows": out,
        }
        stash_result(ctx, "select_columns", args.model_dump(), result)
        return result
