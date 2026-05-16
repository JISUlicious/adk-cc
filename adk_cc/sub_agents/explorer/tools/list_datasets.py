"""ListDatasetsTool — return every dataset known to the registry."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta


class _NoArgs(BaseModel):
    pass


class ListDatasetsTool(AdkCcTool):
    stage: ClassVar[str] = "explore"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="list_datasets",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _NoArgs
    description: ClassVar[str] = (
        "List every dataset available to the agent. EXPLORE-stage tool: "
        "call this FIRST so you know what you can work with."
    )

    async def _execute(self, args: _NoArgs, ctx: Any) -> dict[str, Any]:
        names = datasets.list_dataset_names()
        return {"status": "ok", "datasets": names, "count": len(names)}
