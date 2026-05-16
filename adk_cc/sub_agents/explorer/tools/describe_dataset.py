"""DescribeDatasetTool — schema + numeric ranges for one dataset."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta


class _Args(BaseModel):
    name: str = Field(..., description="Dataset name to describe.")


class DescribeDatasetTool(AdkCcTool):
    stage: ClassVar[str] = "explore"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="describe_dataset",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Return row count, column names + types, and value ranges for one "
        "dataset. EXPLORE-stage tool: use this to understand structure "
        "before planning an analysis."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)
        schema = datasets.schema(args.name)
        numeric_cols = [c for c, t in schema.items() if t in {"int", "float"}]
        ranges: dict[str, dict[str, Any]] = {}
        for col in numeric_cols:
            values = [r[col] for r in rows if isinstance(r.get(col), (int, float))]
            if values:
                ranges[col] = {"min": min(values), "max": max(values)}
        return {
            "status": "ok",
            "name": args.name,
            "row_count": len(rows),
            "columns": schema,
            "numeric_ranges": ranges,
        }
