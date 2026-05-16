"""ProfileDatasetTool — numeric profile (mean/median/stddev/quartiles +
null counts) for every column of one dataset."""

from __future__ import annotations

import statistics
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import stash_result


class _Args(BaseModel):
    name: str = Field(..., description="Dataset name.")


def _quartiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    s = sorted(values)
    return {
        "q1": float(s[len(s) // 4]),
        "median": float(statistics.median(s)),
        "q3": float(s[(3 * len(s)) // 4]),
    }


class ProfileDatasetTool(AdkCcTool):
    stage: ClassVar[str] = "explore"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="profile_dataset",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Numeric profile of a dataset: per-column mean / median / stddev "
        "/ quartiles, plus null counts. Use this before planning to "
        "spot data-quality issues or skews you should account for."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)
        if not rows:
            return {"status": "ok", "name": args.name, "row_count": 0, "columns": {}}
        cols: dict[str, dict[str, Any]] = {}
        for col in rows[0].keys():
            values = [r.get(col) for r in rows]
            numeric = [v for v in values if isinstance(v, (int, float))]
            null_count = sum(1 for v in values if v is None)
            stats: dict[str, Any] = {
                "type": type(rows[0][col]).__name__,
                "null_count": null_count,
            }
            if numeric:
                stats.update(
                    {
                        "mean": round(statistics.mean(numeric), 4),
                        "stddev": (
                            round(statistics.stdev(numeric), 4)
                            if len(numeric) > 1
                            else 0.0
                        ),
                        **_quartiles([float(v) for v in numeric]),
                    }
                )
            cols[col] = stats
        result = {
            "status": "ok",
            "name": args.name,
            "row_count": len(rows),
            "columns": cols,
        }
        stash_result(ctx, "profile_dataset", args.model_dump(), result)
        return result
