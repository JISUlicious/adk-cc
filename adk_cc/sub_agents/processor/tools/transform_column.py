"""TransformColumnTool — element-wise numeric transform on one column."""

from __future__ import annotations

import math
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import stash_result

_NUMERIC_OPS: dict[str, Any] = {
    "log10": lambda v: math.log10(v) if v > 0 else None,
    "abs": abs,
    "negate": lambda v: -v,
    "double": lambda v: v * 2,
    "halve": lambda v: v / 2,
}


class _Args(BaseModel):
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
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Apply log10 / abs / negate / double / halve element-wise to a "
        "numeric column. Returns the transformed column alongside the "
        "original; does not mutate the registry."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
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
        stash_result(ctx, "transform_column", args.model_dump(), result)
        return result
