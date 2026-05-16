"""PeekDatasetTool — return the first N rows of a dataset."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta


class _Args(BaseModel):
    name: str = Field(..., description="Dataset name to peek.")
    n: int = Field(3, ge=1, le=20, description="Number of rows to return.")


class PeekDatasetTool(AdkCcTool):
    stage: ClassVar[str] = "explore"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="peek_dataset",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Return the first N rows (default 3) of a dataset. EXPLORE-stage "
        "tool: sample values to confirm assumptions before acting."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)[: args.n]
        return {"status": "ok", "name": args.name, "rows": rows}
