"""RenderBarChartTool — horizontal ASCII bar chart from one dataset."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import stash_result


class _Args(BaseModel):
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
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Render an ASCII horizontal bar chart from a dataset's "
        "label_col / value_col pair. Returns the chart as a multi-line "
        "string. Use this to summarize a grouping at the end of ACT."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
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
        stash_result(ctx, "render_bar_chart", args.model_dump(), result)
        return result
