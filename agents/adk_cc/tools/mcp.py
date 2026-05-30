"""MCP toolset factory.

Wraps `google.adk.tools.mcp_tool.McpToolset` (the current class — the
all-caps `MCPToolset` is deprecated in 1.31.1). The permission plugin
already applies to MCP tools (they're `BaseTool` instances), but they
don't carry an `AdkCcTool.meta`, so they hit the plugin's "non-AdkCcTool
passthrough" branch by default. Tighten this by adding deny rules
targeting `mcp__<server>__*` patterns:

    PermissionRule(source=POLICY, behavior=DENY,
                   tool_name="mcp__github__*", rule_content=None)

The `tool_name_prefix` argument groups tools by server so deny rules
can target a server cleanly.

Operators register MCP servers by importing this module and calling
`make_mcp_toolset(...)`, then adding the result to an agent's `tools=`
list. adk-cc itself wires no defaults — MCP servers are operator policy.
"""

from __future__ import annotations

from typing import Any, Optional

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.mcp_tool import McpToolset

from .save_mcp_resource_as_artifact import SaveMcpResourceAsArtifactTool


def make_mcp_toolset(
    *,
    server_name: str,
    connection_params: Any,
    tool_filter: list[str] | None = None,
    require_confirmation: bool = False,
    save_resources_as_artifacts: bool = False,
    use_mcp_resources: bool = False,
) -> BaseToolset:
    """Create an McpToolset bound to a single server.

    Args:
      server_name: Used as the `tool_name_prefix` so deny rules can target
        `mcp__<server_name>__*`.
      connection_params: Passed through to McpToolset; the value depends
        on the transport (`StdioServerParameters`, `SseConnectionParams`,
        `StreamableHTTPConnectionParams`, etc.).
      tool_filter: Optional subset of tool names to expose; None = all.
      require_confirmation: If True, every MCP tool call goes through
        ADK's request_confirmation flow. Cheaper than per-tool deny rules
        when you want a blanket "ask before any MCP write".
      save_resources_as_artifacts: If True, expose an extra
        `save_resource_as_artifact` tool (prefixed `mcp__<server>__`)
        bound to this server, so the agent can persist a named resource
        into the artifact store. Does NOT imply `use_mcp_resources` — set
        that too if you also want the agent to auto-discover resource
        names from the catalog (otherwise the model must already know the
        name, e.g. from a prior tool call).
      use_mcp_resources: Pass-through to ADK's McpToolset — adds the
        built-in `load_mcp_resource` tool and injects the server's
        resource list into the agent's context.

    Returns:
      A toolset ready to drop into `LlmAgent(tools=[...])`.
    """
    base = McpToolset(
        connection_params=connection_params,
        tool_filter=tool_filter,
        tool_name_prefix=f"mcp__{server_name}__",
        require_confirmation=require_confirmation,
        use_mcp_resources=use_mcp_resources,
    )
    if not save_resources_as_artifacts:
        return base
    return _ArtifactSavingMcpToolset(base, server_name)


class _ArtifactSavingMcpToolset(BaseToolset):
    """Wraps an `McpToolset`, appending a `save_resource_as_artifact` tool.

    The wrapper — not the inner toolset — is what the runner holds, so its
    `tool_name_prefix` is the one `get_tools_with_prefix` applies. We hoist
    the inner's prefix onto the wrapper and null the inner's, so BOTH the
    real MCP tools and our appended tool get exactly one `mcp__<server>__`
    prefix (no double-prefixing).
    """

    def __init__(self, inner: McpToolset, server_name: str) -> None:
        super().__init__(tool_name_prefix=inner.tool_name_prefix)
        inner.tool_name_prefix = None
        self._inner = inner
        self._server_name = server_name

    async def get_tools(
        self, readonly_context: Optional[ReadonlyContext] = None
    ) -> list[BaseTool]:
        tools = list(await self._inner.get_tools(readonly_context))
        tools.append(
            SaveMcpResourceAsArtifactTool(
                mcp_toolset=self._inner, server_name=self._server_name
            )
        )
        return tools

    async def close(self) -> None:
        await self._inner.close()
