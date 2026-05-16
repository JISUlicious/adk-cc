"""EXPLORE-stage tools: read-only dataset surface for the agent.

Every tool here carries `stage = "explore"` so `StageGuardPlugin` can
identify it as belonging to the gather phase.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from . import datasets
from .base import AdkCcTool, ToolMeta


# --- list_datasets -------------------------------------------------


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


# --- describe_dataset ----------------------------------------------


class _DescribeArgs(BaseModel):
    name: str = Field(..., description="Dataset name to describe.")


class DescribeDatasetTool(AdkCcTool):
    stage: ClassVar[str] = "explore"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="describe_dataset",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _DescribeArgs
    description: ClassVar[str] = (
        "Return row count, column names + types, and value ranges for one "
        "dataset. EXPLORE-stage tool: use this to understand structure "
        "before planning an analysis."
    )

    async def _execute(self, args: _DescribeArgs, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)
        schema = datasets.schema(args.name)
        numeric_cols = [c for c, t in schema.items() if t in {"int", "float"}]
        ranges: dict[str, dict[str, Any]] = {}
        for col in numeric_cols:
            values = [r[col] for r in rows if isinstance(r.get(col), (int, float))]
            if values:
                ranges[col] = {"min": min(values), "max": max(values)}
        return {
            "status": "ok",
            "name": args.name,
            "row_count": len(rows),
            "columns": schema,
            "numeric_ranges": ranges,
        }


# --- peek_dataset --------------------------------------------------


class _PeekArgs(BaseModel):
    name: str = Field(..., description="Dataset name to peek.")
    n: int = Field(3, ge=1, le=20, description="Number of rows to return.")


class PeekDatasetTool(AdkCcTool):
    stage: ClassVar[str] = "explore"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="peek_dataset",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _PeekArgs
    description: ClassVar[str] = (
        "Return the first N rows (default 3) of a dataset. EXPLORE-stage "
        "tool: sample values to confirm assumptions before acting."
    )

    async def _execute(self, args: _PeekArgs, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)[: args.n]
        return {"status": "ok", "name": args.name, "rows": rows}
