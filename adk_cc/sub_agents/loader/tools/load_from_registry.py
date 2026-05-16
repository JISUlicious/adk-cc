"""LoadFromRegistryTool — pulls a dataset out of the in-memory
registry that ships with the demo. Cheapest path; preferred when
the coordinator has a known dataset name."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import record_load


class _Args(BaseModel):
    name: str = Field(..., description="Registry dataset name.")


class LoadFromRegistryTool(AdkCcTool):
    stage: ClassVar[str] = "explore"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="load_from_registry",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Bring a dataset from the in-memory registry into the working set. "
        "Use this when the dataset name is one the system already knows "
        "about (e.g. 'sales_q1'). Cheapest source."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {
                "status": "not_found",
                "name": args.name,
                "hint": f"Known names: {datasets.list_dataset_names()}",
            }
        row_count = len(datasets.get(args.name))
        record_load(ctx, "registry", args.name, row_count)
        return {
            "status": "ok",
            "source": "registry",
            "name": args.name,
            "row_count": row_count,
            "columns": list(datasets.schema(args.name).keys()),
        }
