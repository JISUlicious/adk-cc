"""Minimal FastMCP server exposing CSV data — e2e fixture for adk-cc.

Serves the same dataset two ways so BOTH artifact-bridge patterns can be
exercised against one server:

  - **Resource** `csv://orders` (for Pattern A): the agent reads it by
    name via `read_resource`, and `save_resource_as_artifact` persists it.
    Exposed as a real MCP resource (text/csv).

  - **Tool** `export_orders` (for Pattern C): returns a bounded preview
    (TextContent JSON: first rows + total) PLUS a `resource_link` to the
    full CSV, tagged `audience:["user"]`. The link uri is `csv://orders`
    (same data); when adk-cc's McpExportArtifactPlugin can't fetch the
    custom scheme it logs+skips (v1), so for a fetchable demo the tool
    ALSO supports returning an `https://`-less inline path — but here we
    keep it spec-correct with a resource_link + a fallback embedded
    resource the plugin CAN persist (see `embed=True`).

Run over stdio (default): `python tests/fixtures/csv_mcp_server.py`.
adk-cc wires it via StdioServerParameters.
"""

from __future__ import annotations

import csv
import io

from mcp.server.fastmcp import FastMCP
from mcp.types import Annotations, EmbeddedResource, ResourceLink, TextContent, TextResourceContents

mcp = FastMCP("csv-demo")

# --- the dataset ----------------------------------------------------------
_HEADER = ["order_id", "customer", "region", "amount", "status"]
_ROWS = [
    [1001, "Alice", "us-east", 42.00, "paid"],
    [1002, "Bob", "us-west", 17.50, "paid"],
    [1003, "Cara", "eu-west", 88.10, "refunded"],
    [1004, "Dan", "ap-south", 5.25, "paid"],
    [1005, "Eve", "us-east", 120.00, "pending"],
    [1006, "Frank", "eu-west", 63.75, "paid"],
    [1007, "Grace", "us-west", 9.99, "paid"],
    [1008, "Heidi", "ap-south", 250.00, "paid"],
]
_CSV_URI = "csv://orders"


def _csv_text() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_HEADER)
    w.writerows(_ROWS)
    return buf.getvalue()


# --- Pattern A: a readable resource --------------------------------------
@mcp.resource(
    _CSV_URI,
    name="orders",
    title="Orders export (CSV)",
    mime_type="text/csv",
    description="All orders as CSV (8 rows x 5 cols).",
)
def orders_resource() -> str:
    return _csv_text()


# --- Pattern C: an export tool that returns a resource_link --------------
@mcp.tool(
    name="export_orders",
    title="Export orders to CSV",
    description="Run the orders export and return a downloadable CSV link plus a preview.",
)
def export_orders(embed: bool = False) -> list:
    """Return a bounded preview + a link (or embedded copy) of the full CSV.

    Args:
      embed: when True, also include an EmbeddedResource carrying the full
        CSV inline (audience:user) — this is what the v1 auto-persist
        plugin can save directly (a custom-scheme resource_link would be
        skipped). Defaults to False (link-only, spec-canonical).
    """
    text = _csv_text()
    preview = {
        "columns": _HEADER,
        "rows": _ROWS[:3],
        "total": len(_ROWS),
        "has_more": len(_ROWS) > 3,
    }
    import json

    blocks: list = [TextContent(type="text", text=json.dumps(preview))]
    blocks.append(
        ResourceLink(
            type="resource_link",
            uri=_CSV_URI,
            name="orders.csv",
            mimeType="text/csv",
            description=f"Full export: {len(_ROWS)} rows",
            size=len(text.encode()),
            annotations=Annotations(audience=["user"], priority=0.3),
        )
    )
    if embed:
        blocks.append(
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=_CSV_URI, mimeType="text/csv", text=text
                ),
                annotations=Annotations(audience=["user"], priority=0.3),
            )
        )
    return blocks


if __name__ == "__main__":
    mcp.run()  # stdio
