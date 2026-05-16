"""Visualizer tools (owned by the `visualizer` sub-agent).

This branch runs in a text-only environment — no image rendering. The
visualizer specialist produces ASCII charts and markdown tables that
are useful both for the model's reasoning chain (it can re-read its
own chart) and for the final user-facing reply.
"""

from __future__ import annotations

import statistics
import time
from typing import Any, ClassVar

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


# --- render_bar_chart ----------------------------------------------


class _BarArgs(BaseModel):
    name: str = Field(..., description="Dataset name.")
    label_col: str = Field(..., description="Column to use as bar labels.")
    value_col: str = Field(..., description="Numeric column for bar lengths.")
    width: int = Field(40, ge=5, le=120, description="Max bar width in chars.")


class RenderBarChartTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="render_bar_chart",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _BarArgs
    description: ClassVar[str] = (
        "Render an ASCII horizontal bar chart from a dataset's "
        "label_col / value_col pair. Returns the chart as a multi-line "
        "string. Use this to summarize a grouping at the end of ACT."
    )

    async def _execute(self, args: _BarArgs, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)
        labels: list[str] = []
        values: list[float] = []
        for r in rows:
            v = r.get(args.value_col)
            if not isinstance(v, (int, float)):
                continue
            labels.append(str(r.get(args.label_col)))
            values.append(float(v))
        if not values:
            return {"status": "error", "error": "no numeric values found"}
        max_v = max(values)
        max_label = max(len(label) for label in labels)
        lines: list[str] = []
        for label, val in zip(labels, values):
            bar = "█" * max(1, round((val / max_v) * args.width))
            lines.append(f"{label.ljust(max_label)} │ {bar} {val:,.0f}")
        chart = "\n".join(lines)
        result = {
            "status": "ok",
            "name": args.name,
            "label_col": args.label_col,
            "value_col": args.value_col,
            "chart": chart,
            "rows_rendered": len(values),
        }
        _stash(ctx, "render_bar_chart", args.model_dump(), result)
        return result


# --- render_table --------------------------------------------------


class _TableArgs(BaseModel):
    name: str = Field(..., description="Dataset name.")
    max_rows: int = Field(10, ge=1, le=50, description="Row cap.")


class RenderTableTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="render_table",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _TableArgs
    description: ClassVar[str] = (
        "Render a dataset as a markdown-style table (up to max_rows). "
        "Use for the final tabular summary."
    )

    async def _execute(self, args: _TableArgs, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)[: args.max_rows]
        if not rows:
            return {"status": "ok", "name": args.name, "table": ""}
        cols = list(rows[0].keys())
        header = " | ".join(cols)
        sep = " | ".join(["---"] * len(cols))
        body = "\n".join(" | ".join(str(r.get(c, "")) for c in cols) for r in rows)
        table = f"{header}\n{sep}\n{body}"
        result = {
            "status": "ok",
            "name": args.name,
            "rows_rendered": len(rows),
            "table": table,
        }
        _stash(ctx, "render_table", args.model_dump(), result)
        return result


# --- summarize_distribution ----------------------------------------


class _DistArgs(BaseModel):
    name: str = Field(..., description="Dataset name.")
    column: str = Field(..., description="Numeric column to summarize.")


class SummarizeDistributionTool(AdkCcTool):
    stage: ClassVar[str] = "act"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="summarize_distribution",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _DistArgs
    description: ClassVar[str] = (
        "Mean / median / stddev / min / max summary of one numeric "
        "column. Cheaper than profile_dataset when you only need one "
        "column's stats post-filter."
    )

    async def _execute(self, args: _DistArgs, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {"status": "not_found", "name": args.name}
        rows = datasets.get(args.name)
        values = [r.get(args.column) for r in rows if isinstance(r.get(args.column), (int, float))]
        if not values:
            return {"status": "error", "error": f"no numeric values in {args.column!r}"}
        summary = {
            "n": len(values),
            "mean": round(statistics.mean(values), 4),
            "median": round(statistics.median(values), 4),
            "stddev": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
            "min": float(min(values)),
            "max": float(max(values)),
        }
        result = {
            "status": "ok",
            "name": args.name,
            "column": args.column,
            "summary": summary,
        }
        _stash(ctx, "summarize_distribution", args.model_dump(), result)
        return result
