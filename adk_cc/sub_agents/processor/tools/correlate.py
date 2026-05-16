"""CorrelateTool — Pearson r between two numeric columns."""

from __future__ import annotations

import math
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import stash_result


class _Args(BaseModel):
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
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Pearson correlation coefficient between two numeric columns "
        "of the same dataset. Result in [-1.0, 1.0]."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
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
        stash_result(ctx, "correlate", args.model_dump(), result)
        return result
