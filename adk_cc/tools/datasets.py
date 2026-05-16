"""In-memory dataset registry for the data-science agent.

This branch runs on a web server with no filesystem tools — datasets
are held as plain Python lists of dicts in this module-level
`_REGISTRY`. A production deployment would back this with whatever
storage the operator already runs (Postgres, Parquet on S3, etc.),
exposing the same shape: `list_dataset_names()`, `get(name)`, etc.

The seed data is intentionally tiny and deterministic so the example
agent's scripted-LLM walkthrough produces verifiable numbers.
"""

from __future__ import annotations

from typing import Any


# name -> list[dict[column, value]]
_REGISTRY: dict[str, list[dict[str, Any]]] = {
    "sales_q1": [
        {"region": "north",  "rep": "Alice",  "deals": 12, "revenue": 240_000},
        {"region": "north",  "rep": "Bob",    "deals":  8, "revenue": 180_000},
        {"region": "south",  "rep": "Carol",  "deals": 15, "revenue": 310_000},
        {"region": "south",  "rep": "Dave",   "deals": 11, "revenue": 220_000},
        {"region": "west",   "rep": "Eve",    "deals":  9, "revenue": 195_000},
        {"region": "west",   "rep": "Frank",  "deals": 14, "revenue": 295_000},
    ],
    "sales_q2": [
        {"region": "north",  "rep": "Alice",  "deals": 14, "revenue": 280_000},
        {"region": "north",  "rep": "Bob",    "deals":  6, "revenue": 130_000},
        {"region": "south",  "rep": "Carol",  "deals": 17, "revenue": 355_000},
        {"region": "south",  "rep": "Dave",   "deals":  9, "revenue": 190_000},
        {"region": "west",   "rep": "Eve",    "deals": 12, "revenue": 260_000},
        {"region": "west",   "rep": "Frank",  "deals": 13, "revenue": 275_000},
    ],
    "customers": [
        {"id": 1, "segment": "enterprise", "annual_spend": 500_000, "tenure_years": 5},
        {"id": 2, "segment": "smb",        "annual_spend":  45_000, "tenure_years": 2},
        {"id": 3, "segment": "enterprise", "annual_spend": 620_000, "tenure_years": 7},
        {"id": 4, "segment": "smb",        "annual_spend":  60_000, "tenure_years": 1},
        {"id": 5, "segment": "midmarket",  "annual_spend": 180_000, "tenure_years": 3},
    ],
}


def list_dataset_names() -> list[str]:
    return sorted(_REGISTRY.keys())


def get(name: str) -> list[dict[str, Any]]:
    """Return a defensive copy so callers can't mutate the registry."""
    if name not in _REGISTRY:
        raise KeyError(name)
    return [dict(row) for row in _REGISTRY[name]]


def exists(name: str) -> bool:
    return name in _REGISTRY


def schema(name: str) -> dict[str, str]:
    """Inferred {column: type_name} for the first row of `name`."""
    rows = _REGISTRY.get(name) or []
    if not rows:
        return {}
    return {col: type(val).__name__ for col, val in rows[0].items()}
