"""ACT-stage tools: data-science transforms the agent calls to actually
produce the answer.

These are pure-function transformations over the in-memory dataset
registry. No mutation of the registry. Each result also gets stashed
in `state["temp:loop_results"]` so `verify_completion` can replay
what the agent produced when checking the conclusion.
"""

from __future__ import annotations

import math
import time
from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from . import datasets
from .base import AdkCcTool, ToolMeta

_RESULTS_KEY = "temp:loop_results"


def _stash_result(ctx: Any, tool_name: str, args: dict[str, Any], result: Any) -> None:
    """Persist this acting-tool's output into session state so the
    verifier can audit what the agent actually computed."""
    log = ctx.state.get(_RESULTS_KEY) or []
    log.append(
        {
            "ts": time.time(),
            "tool": tool_name,
            "args": args,
            "result": result,
        }
    )
    ctx.state[_RESULTS_KEY] = log


# --- filter_dataset ------------------------------------------------


class _FilterArgs(BaseModel):
    name: str = Field(..., description="Dataset name to filter.")
    column: str = Field(..., description="Column to filter on.")
    op: Literal["==", "!=", ">", ">=", "<", "<="] = Field(
        ..., description="Comparison operator."
    )
    value: Any = Field(..., description="RHS value for the comparison.")


_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


class FilterDatasetTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="filter_dataset",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _FilterArgs
    description: ClassVar[str] = (
        "Return the subset of `name` where `column op value` holds. "
        "Does not mutate the dataset."
    )

    async def _execute(self, args: _FilterArgs, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        try:
            op = _OPS[args.op]
        except KeyError:
            return {"status": "error", "error": f"unknown op {args.op!r}"}
        rows = datasets.get(args.name)
        try:
            kept = [r for r in rows if args.column in r and op(r[args.column], args.value)]
        except TypeError as exc:
            return {"status": "error", "error": f"type mismatch: {exc}"}
        result = {
            "status": "ok",
            "name": args.name,
            "column": args.column,
            "op": args.op,
            "value": args.value,
            "rows_in": len(rows),
            "rows_kept": len(kept),
            "rows": kept,
        }
        _stash_result(ctx, "filter_dataset", args.model_dump(), result)
        return result


# --- aggregate_dataset ---------------------------------------------


class _AggregateArgs(BaseModel):
    name: str = Field(..., description="Dataset name to aggregate.")
    group_by: Optional[str] = Field(
        None,
        description="Column to group by, or omit for a single-row global aggregate.",
    )
    metric: str = Field(..., description="Numeric column to aggregate.")
    op: Literal["sum", "avg", "min", "max", "count"] = Field(
        ..., description="Aggregation operation."
    )


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


class AggregateDatasetTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="aggregate_dataset",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _AggregateArgs
    description: ClassVar[str] = (
        "Aggregate a numeric column with sum/avg/min/max/count, optionally "
        "grouped by another column. Returns one row per group "
        "(or one global row when group_by is omitted)."
    )

    async def _execute(self, args: _AggregateArgs, ctx: Any) -> dict[str, Any]:
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
        _stash_result(ctx, "aggregate_dataset", args.model_dump(), result)
        return result


# --- correlate -----------------------------------------------------


class _CorrelateArgs(BaseModel):
    name: str = Field(..., description="Dataset name.")
    col_a: str = Field(..., description="First numeric column.")
    col_b: str = Field(..., description="Second numeric column.")


class CorrelateTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="correlate",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _CorrelateArgs
    description: ClassVar[str] = (
        "Pearson correlation coefficient between two numeric columns "
        "of the same dataset. Result in [-1.0, 1.0]."
    )

    async def _execute(self, args: _CorrelateArgs, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)
        pairs = [
            (float(r[args.col_a]), float(r[args.col_b]))
            for r in rows
            if isinstance(r.get(args.col_a), (int, float))
            and isinstance(r.get(args.col_b), (int, float))
        ]
        if len(pairs) < 2:
            return {"status": "error", "error": "need >= 2 numeric pairs"}
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        cov = sum((x - mx) * (y - my) for x, y in pairs)
        vx = sum((x - mx) ** 2 for x in xs)
        vy = sum((y - my) ** 2 for y in ys)
        denom = math.sqrt(vx * vy)
        if denom == 0:
            return {"status": "ok", "name": args.name, "r": 0.0, "n": len(pairs)}
        r = cov / denom
        result = {
            "status": "ok",
            "name": args.name,
            "col_a": args.col_a,
            "col_b": args.col_b,
            "r": round(r, 6),
            "n": len(pairs),
        }
        _stash_result(ctx, "correlate", args.model_dump(), result)
        return result
