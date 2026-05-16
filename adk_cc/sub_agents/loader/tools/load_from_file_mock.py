"""LoadFromFileMockTool — pretend parquet/csv backend. Production
would swap this for an S3 / GCS / local-filesystem load."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import record_load

_FILE_PATH_TABLE = {
    "/data/sales_q1.parquet": "sales_q1",
    "/data/sales_q2.parquet": "sales_q2",
    "/data/customers.csv": "customers",
}


class _Args(BaseModel):
    path: str = Field(..., description="File path to load (parquet/csv mock).")


class LoadFromFileMockTool(AdkCcTool):
    stage: ClassVar[str] = "explore"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="load_from_file_mock",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Pretend object-store / filesystem backend. Pass a path that "
        "matches a known fixture to load it. Used to show the agent "
        "can route a load through a file source."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
        name = _FILE_PATH_TABLE.get(args.path)
        if name is None:
            return {
                "status": "not_found",
                "path": args.path,
                "hint": f"Supported paths: {list(_FILE_PATH_TABLE.keys())}",
            }
        row_count = len(datasets.get(name))
        record_load(ctx, "file_mock", name, row_count)
        return {
            "status": "ok",
            "source": "file_mock",
            "path": args.path,
            "name": name,
            "row_count": row_count,
        }
