"""Pre/post-processing tools (owned by the `processor` sub-agent).

`filter_dataset`, `aggregate_dataset`, `correlate` are pulled in from
`acting.py`. This module adds the transforms that make the processor
useful: dropping rows with missing values, projecting columns, and
applying a column-level transform.

All transforms operate on a defensive copy of the registry row and
return the result back to the caller — never mutate the underlying
registry. Each call also gets stashed in `state["temp:loop_results"]`
so the verifier can audit the chain.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from . import datasets
from .base import AdkCcTool, ToolMeta

_RESULTS_KEY = "temp:loop_results"


def _stash(ctx: Any, tool_name: str, args: dict[str, Any], result: Any) -> None:
    log = ctx.state.get(_RESULTS_KEY) or []
    log.append(
        {"ts": time.time(), "tool": tool_name, "args": args, "result": result}
    )
    ctx.state[_RESULTS_KEY] = log


# --- drop_na -------------------------------------------------------


class _DropNaArgs(BaseModel):
    name: str = Field(..., description="Dataset name.")
    column: str = Field(..., description="Column whose null values trigger a row drop.")


class DropNaTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="drop_na",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _DropNaArgs
    description: ClassVar[str] = (
        "Return rows of `name` where `column` is not None / missing. "
        "Reports rows_dropped so the agent can flag data-quality issues."
    )

    async def _execute(self, args: _DropNaArgs, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)
        kept = [r for r in rows if r.get(args.column) is not None]
        result = {
            "status": "ok",
            "name": args.name,
            "column": args.column,
            "rows_in": len(rows),
            "rows_kept": len(kept),
            "rows_dropped": len(rows) - len(kept),
        }
        _stash(ctx, "drop_na", args.model_dump(), result)
        return result


# --- transform_column ----------------------------------------------


_NUMERIC_OPS: dict[str, Any] = {
    "log10": lambda v: __import__("math").log10(v) if v > 0 else None,
    "abs": abs,
    "negate": lambda v: -v,
    "double": lambda v: v * 2,
    "halve": lambda v: v / 2,
}


class _TransformArgs(BaseModel):
    name: str = Field(..., description="Dataset name.")
    column: str = Field(..., description="Numeric column to transform.")
    op: Literal["log10", "abs", "negate", "double", "halve"] = Field(
        ..., description="Element-wise operation to apply."
    )


class TransformColumnTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="transform_column",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _TransformArgs
    description: ClassVar[str] = (
        "Apply log10 / abs / negate / double / halve element-wise to a "
        "numeric column. Returns the transformed column alongside the "
        "original; does not mutate the registry."
    )

    async def _execute(self, args: _TransformArgs, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)
        fn = _NUMERIC_OPS[args.op]
        out: list[dict[str, Any]] = []
        for r in rows:
            v = r.get(args.column)
            if not isinstance(v, (int, float)):
                out.append({"original": v, "transformed": None})
            else:
                try:
                    out.append({"original": v, "transformed": fn(v)})
                except Exception as exc:
                    out.append({"original": v, "transformed": None, "error": str(exc)})
        result = {
            "status": "ok",
            "name": args.name,
            "column": args.column,
            "op": args.op,
            "values": out,
        }
        _stash(ctx, "transform_column", args.model_dump(), result)
        return result


# --- select_columns ------------------------------------------------


class _SelectColsArgs(BaseModel):
    name: str = Field(..., description="Dataset name.")
    columns: list[str] = Field(..., min_length=1, description="Columns to keep.")


class SelectColumnsTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="select_columns",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _SelectColsArgs
    description: ClassVar[str] = (
        "Project a dataset down to a subset of columns. Returns the "
        "narrowed rows; does not mutate the registry."
    )

    async def _execute(self, args: _SelectColsArgs, ctx: Any) -> dict[str, Any]:
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
        _stash(ctx, "select_columns", args.model_dump(), result)
        return result
