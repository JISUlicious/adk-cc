"""LoadFromDbMockTool — pretend SQL backend. Production would
swap this body for a real `SELECT` against your DB driver."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ....tools import datasets
from ....tools.base import AdkCcTool, ToolMeta
from ....tools.loop_state import record_load

_DB_QUERY_TABLE = {
    "SELECT * FROM sales WHERE quarter='Q1'": "sales_q1",
    "SELECT * FROM sales WHERE quarter='Q2'": "sales_q2",
    "SELECT * FROM customers": "customers",
}


class _Args(BaseModel):
    query: str = Field(
        ...,
        description=(
            "SQL-ish query string. In this mock the query is matched "
            "against a small table; the body would normally execute "
            "against your actual DB."
        ),
    )


class LoadFromDbMockTool(AdkCcTool):
    stage: ClassVar[str] = "explore"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="load_from_db_mock",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = (
        "Pretend SQL backend. Pass a query that matches one of the "
        "supported patterns to load a dataset by query. Used to show "
        "the agent can route a load through a DB source."
    )

    async def _execute(self, args: _Args, ctx: Any) -> dict[str, Any]:
        name = _DB_QUERY_TABLE.get(args.query.strip())
        if name is None:
            return {
                "status": "no_match",
                "query": args.query,
                "hint": f"Supported queries: {list(_DB_QUERY_TABLE.keys())}",
            }
        row_count = len(datasets.get(name))
        record_load(ctx, "db_mock", name, row_count)
        return {
            "status": "ok",
            "source": "db_mock",
            "query": args.query,
            "name": name,
            "row_count": row_count,
        }
