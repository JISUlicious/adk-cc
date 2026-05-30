"""Model tool: read a named MCP resource and save it as an artifact.

Pattern A of the MCP → artifact bridge. The agent calls this to fetch a
resource the MCP server exposes (via `resources/read`) and persist it to
ADK's artifact service (S3/MinIO-backed) so the user can download it and
it outlives the session.

Bound to its owning `McpToolset` — a resource name is only meaningful
relative to one server's resource list, and `read_resource(name)`
resolves name→uri via that server then reads through its session. This
mirrors ADK's own `LoadMcpResourceTool(mcp_toolset=self)`, so the tool is
PRODUCED BY THE TOOLSET (see tools/mcp.py + tools/mcp_tenant.py), not
registered as a standalone coordinator tool. It subclasses ADK's
`BaseTool` directly (not `AdkCcTool`) because it needs the toolset handle
injected at construction.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import ValidationError

from ._artifact import save_part_as_artifact
from ._mcp_content import mcp_content_to_part
from .schemas import SaveMcpResourceAsArtifactArgs

_log = logging.getLogger(__name__)


def _safe_name(resource_name: str) -> str:
    """Derive a flat artifact filename from a resource name / URI.

    Strips a `scheme://` prefix and replaces path separators and other
    filesystem-hostile chars so `db://schema/users` → `schema_users`.
    """
    name = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", resource_name)
    name = name.strip("/")
    name = re.sub(r"[/\\:\s]+", "_", name)
    name = re.sub(r"[^A-Za-z0-9._\-]", "", name)
    return name or "resource"


class SaveMcpResourceAsArtifactTool(BaseTool):
    """Reads one named resource from the bound MCP server → artifact store."""

    def __init__(self, *, mcp_toolset: Any, server_name: str) -> None:
        super().__init__(
            name="save_resource_as_artifact",
            description=(
                "Read a named resource from this MCP server and save it as "
                "a downloadable artifact in the chat UI / artifact store. "
                "Use when the user wants to keep or download an MCP "
                "resource (file, report, export, image). `scope` 'session' "
                "(default) ties it to this session and shows a download "
                "chip; 'user' persists across the user's future sessions."
            ),
        )
        self._mcp_toolset = mcp_toolset
        self._server_name = server_name

    def _get_declaration(self) -> types.FunctionDeclaration:
        schema = SaveMcpResourceAsArtifactArgs.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema=schema,
        )

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        try:
            a = SaveMcpResourceAsArtifactArgs.model_validate(args)
        except ValidationError as e:
            return {"status": "input_validation_error", "errors": e.errors()}

        # 1. Read the resource (list of MCP content objects).
        try:
            contents = await self._mcp_toolset.read_resource(a.resource_name)
        except ValueError as e:
            # read_resource raises ValueError when the name isn't in the
            # server's resource list / has no URI.
            return {"status": "not_found", "error": str(e)}
        except Exception as e:  # noqa: BLE001 — transport / server errors
            return {
                "status": "error",
                "error": f"read_resource({a.resource_name!r}) failed: {e}",
            }

        if not contents:
            return {
                "status": "error",
                "error": f"resource {a.resource_name!r} returned no contents",
            }

        base = a.filename or _safe_name(a.resource_name)
        multi = len(contents) > 1

        saved: list[dict] = []
        for i, content in enumerate(contents):
            part = mcp_content_to_part(content)
            if part is None:
                return {
                    "status": "error",
                    "error": (
                        f"unsupported content type at index {i} for "
                        f"resource {a.resource_name!r} (neither text nor blob)"
                    ),
                }
            fname = base if not multi else f"{base}.{i}"
            res = await save_part_as_artifact(
                tool_context, filename=fname, part=part, scope=a.scope
            )
            if res.get("status") != "ok":
                return res  # surface the first failure (scope/service/etc.)
            saved.append(res)

        if not multi:
            return saved[0]
        return {
            "status": "ok",
            "resource_name": a.resource_name,
            "scope": (a.scope or "session").lower(),
            "count": len(saved),
            "saved": saved,
        }
