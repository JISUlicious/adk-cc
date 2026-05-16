"""LOAD-stage tools (owned by the `loader` sub-agent).

These simulate the three data sources a production deployment would
expose to the agent — the in-memory registry that ships with the
demo, plus mock DB and mock file backends. Each loader logs the
chosen source into `state["temp:datasets_loaded"]` so downstream
specialists can see what was brought into the working set.

In production the bodies would call into your actual storage layer
(SQLAlchemy, Spark, S3 SDK). The agent's contract — call a loader
during EXPLORE before profiling — stays unchanged.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from . import datasets
from .base import AdkCcTool, ToolMeta

_LOADED_KEY = "temp:datasets_loaded"


def _record_load(ctx: Any, source: str, name: str, row_count: int) -> None:
    log = ctx.state.get(_LOADED_KEY) or []
    log.append(
        {
            "ts": time.time(),
            "source": source,
            "name": name,
            "row_count": row_count,
        }
    )
    ctx.state[_LOADED_KEY] = log


# --- load_from_registry --------------------------------------------


class _LoadRegistryArgs(BaseModel):
    name: str = Field(..., description="Registry dataset name.")


class LoadFromRegistryTool(AdkCcTool):
    stage: ClassVar[str] = "explore"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="load_from_registry",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _LoadRegistryArgs
    description: ClassVar[str] = (
        "Bring a dataset from the in-memory registry into the working set. "
        "Use this when the dataset name is one the system already knows "
        "about (e.g. 'sales_q1'). Cheapest source."
    )

    async def _execute(self, args: _LoadRegistryArgs, ctx: Any) -> dict[str, Any]:
        if not datasets.exists(args.name):
            return {
                "status": "not_found",
                "name": args.name,
                "hint": f"Known names: {datasets.list_dataset_names()}",
            }
        row_count = len(datasets.get(args.name))
        _record_load(ctx, "registry", args.name, row_count)
        return {
            "status": "ok",
            "source": "registry",
            "name": args.name,
            "row_count": row_count,
            "columns": list(datasets.schema(args.name).keys()),
        }


# --- load_from_db_mock ---------------------------------------------


_DB_QUERY_TABLE = {
    "SELECT * FROM sales WHERE quarter='Q1'": "sales_q1",
    "SELECT * FROM sales WHERE quarter='Q2'": "sales_q2",
    "SELECT * FROM customers": "customers",
}


class _LoadDbArgs(BaseModel):
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
    input_model: ClassVar[type[BaseModel]] = _LoadDbArgs
    description: ClassVar[str] = (
        "Pretend SQL backend. Pass a query that matches one of the "
        "supported patterns to load a dataset by query. Used to show "
        "the agent can route a load through a DB source."
    )

    async def _execute(self, args: _LoadDbArgs, ctx: Any) -> dict[str, Any]:
        name = _DB_QUERY_TABLE.get(args.query.strip())
        if name is None:
            return {
                "status": "no_match",
                "query": args.query,
                "hint": f"Supported queries: {list(_DB_QUERY_TABLE.keys())}",
            }
        row_count = len(datasets.get(name))
        _record_load(ctx, "db_mock", name, row_count)
        return {
            "status": "ok",
            "source": "db_mock",
            "query": args.query,
            "name": name,
            "row_count": row_count,
        }


# --- load_from_file_mock -------------------------------------------


_FILE_PATH_TABLE = {
    "/data/sales_q1.parquet": "sales_q1",
    "/data/sales_q2.parquet": "sales_q2",
    "/data/customers.csv": "customers",
}


class _LoadFileArgs(BaseModel):
    path: str = Field(..., description="File path to load (parquet/csv mock).")


class LoadFromFileMockTool(AdkCcTool):
    stage: ClassVar[str] = "explore"
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="load_from_file_mock",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _LoadFileArgs
    description: ClassVar[str] = (
        "Pretend object-store / filesystem backend. Pass a path that "
        "matches a known fixture to load it. Used to show the agent "
        "can route a load through a file source."
    )

    async def _execute(self, args: _LoadFileArgs, ctx: Any) -> dict[str, Any]:
        name = _FILE_PATH_TABLE.get(args.path)
        if name is None:
            return {
                "status": "not_found",
                "path": args.path,
                "hint": f"Supported paths: {list(_FILE_PATH_TABLE.keys())}",
            }
        row_count = len(datasets.get(name))
        _record_load(ctx, "file_mock", name, row_count)
        return {
            "status": "ok",
            "source": "file_mock",
            "path": args.path,
            "name": name,
            "row_count": row_count,
        }
