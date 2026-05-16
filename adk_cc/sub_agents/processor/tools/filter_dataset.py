"""FilterDatasetTool — subset rows by a comparison predicate."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import stash_result

_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


class _Args(BaseModel):
    name: str = Field(..., description="Dataset name to filter.")
    column: str = Field(..., description="Column to filter on.")
    op: Literal["==", "!=", ">", ">=", "<", "<="] = Field(
        ..., description="Comparison operator."
    )
    value: Any = Field(..., description="RHS value for the comparison.")


class FilterDatasetTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="filter_dataset",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Return the subset of `name` where `column op value` holds. "
        "Does not mutate the dataset."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
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
        stash_result(ctx, "filter_dataset", args.model_dump(), result)
        return result
