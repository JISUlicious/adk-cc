"""SummarizeDistributionTool — single-column mean/median/stddev/min/max."""

from __future__ import annotations

import statistics
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import stash_result


class _Args(BaseModel):
    name: str = Field(..., description="Dataset name.")
    column: str = Field(..., description="Numeric column to summarize.")


class SummarizeDistributionTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="summarize_distribution",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Mean / median / stddev / min / max summary of one numeric "
        "column. Cheaper than profile_dataset when you only need one "
        "column's stats post-filter."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)
        values = [r.get(args.column) for r in rows if isinstance(r.get(args.column), (int, float))]
        if not values:
            return {"status": "error", "error": f"no numeric values in {args.column!r}"}
        summary = {
            "n": len(values),
            "mean": round(statistics.mean(values), 4),
            "median": round(statistics.median(values), 4),
            "stddev": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
            "min": float(min(values)),
            "max": float(max(values)),
        }
        result = {
            "status": "ok",
            "name": args.name,
            "column": args.column,
            "summary": summary,
        }
        stash_result(ctx, "summarize_distribution", args.model_dump(), result)
        return result
