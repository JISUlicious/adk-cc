"""DropNaTool — drop rows where a given column is missing/null."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import stash_result


class _Args(BaseModel):
    name: str = Field(..., description="Dataset name.")
    column: str = Field(..., description="Column whose null values trigger a row drop.")


class DropNaTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="drop_na",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Return rows of `name` where `column` is not None / missing. "
        "Reports rows_dropped so the agent can flag data-quality issues."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
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
        stash_result(ctx, "drop_na", args.model_dump(), result)
        return result
