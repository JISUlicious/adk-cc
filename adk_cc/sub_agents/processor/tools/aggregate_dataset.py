"""AggregateDatasetTool — sum / avg / min / max / count grouped by column."""

from __future__ import annotations

from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import stash_result


def _agg(values: list[float], op: str) -> float:
    if op == "count":
        return float(len(values))
    if not values:
        return 0.0
    if op == "sum":
        return float(sum(values))
    if op == "avg":
        return float(sum(values) / len(values))
    if op == "min":
        return float(min(values))
    if op == "max":
        return float(max(values))
    raise ValueError(op)


class _Args(BaseModel):
    name: str = Field(..., description="Dataset name to aggregate.")
    group_by: Optional[str] = Field(
        None,
        description="Column to group by, or omit for a single-row global aggregate.",
    )
    metric: str = Field(..., description="Numeric column to aggregate.")
    op: Literal["sum", "avg", "min", "max", "count"] = Field(
        ..., description="Aggregation operation."
    )


class AggregateDatasetTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="aggregate_dataset",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Aggregate a numeric column with sum/avg/min/max/count, optionally "
        "grouped by another column. Returns one row per group "
        "(or one global row when group_by is omitted)."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)
        groups: dict[Any, list[float]] = {}
        for r in rows:
            v = r.get(args.metric)
            if not isinstance(v, (int, float)):
                continue
            key = r.get(args.group_by) if args.group_by else "_all_"
            groups.setdefault(key, []).append(float(v))
        try:
            buckets = [
                {"group": k, args.op: _agg(v, args.op), "n": len(v)}
                for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))
            ]
        except ValueError as exc:
            return {"status": "error", "error": f"unknown op {exc}"}
        result = {
            "status": "ok",
            "name": args.name,
            "group_by": args.group_by,
            "metric": args.metric,
            "op": args.op,
            "buckets": buckets,
        }
        stash_result(ctx, "aggregate_dataset", args.model_dump(), result)
        return result
