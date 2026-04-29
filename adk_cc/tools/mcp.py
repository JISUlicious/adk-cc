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

from typing import Any

from google.adk.tools.mcp_tool import McpToolset


def make_mcp_toolset(
    *,
    server_name: str,
    connection_params: Any,
    tool_filter: list[str] | None = None,
    require_confirmation: bool = False,
) -> McpToolset:
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

    Returns:
      An McpToolset ready to drop into `LlmAgent(tools=[...])`.
    """
    return McpToolset(
        connection_params=connection_params,
        tool_filter=tool_filter,
        tool_name_prefix=f"mcp__{server_name}__",
        require_confirmation=require_confirmation,
    )
