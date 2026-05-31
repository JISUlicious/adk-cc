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


def connection_params_for(
    transport: str, url: str, *, headers: Optional[dict[str, str]] = None
):
    """Build ADK MCP connection params for a transport + URL/command.

    Single source of the transport→params mapping, shared by the static
    (tools/mcp.py + agent.py) and per-tenant (tools/mcp_tenant.py) wiring.
      - stdio: `url` is the command line (tokenized with shlex).
      - sse / http: `url` is the server URL; `headers` carry auth.
    Raises ValueError on an unknown transport.
    """
    transport = (transport or "stdio").lower()
    if transport == "stdio":
        import shlex

        from mcp import StdioServerParameters

        parts = shlex.split(url)
        return StdioServerParameters(command=parts[0], args=parts[1:])
    if transport == "sse":
        from google.adk.tools.mcp_tool.mcp_session_manager import SseConnectionParams

        return SseConnectionParams(url=url, headers=headers)
    if transport == "http":
        from google.adk.tools.mcp_tool.mcp_session_manager import (
            StreamableHTTPConnectionParams,
        )

        return StreamableHTTPConnectionParams(url=url, headers=headers)
    raise ValueError(f"unknown MCP transport: {transport!r}")

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


def bind_save_tool(inner: McpToolset, server_name: str) -> BaseTool:
    """A `save_resource_as_artifact` tool bound to `inner`, name pre-prefixed.

    Returned with its `.name` and function-declaration name already set to
    `mcp__<server>___save_resource_as_artifact` (the `___` is the `__`
    prefix end + the `_` separator ADK's get_tools_with_prefix inserts), so
    callers can append it ALONGSIDE tools that were already prefixed by
    `inner.get_tools_with_prefix(...)` and everything groups under the same
    server. Used by both the single-tenant wrapper and TenantMcpToolset.
    """
    tool = SaveMcpResourceAsArtifactTool(mcp_toolset=inner, server_name=server_name)
    prefixed = f"mcp__{server_name}___{tool.name}"
    tool.name = prefixed
    _orig_decl = tool._get_declaration

    def _prefixed_decl(_orig=_orig_decl, _name=prefixed):
        decl = _orig()
        if decl is not None:
            decl.name = _name
        return decl

    tool._get_declaration = _prefixed_decl
    return tool


class _ArtifactSavingMcpToolset(BaseToolset):
    """Wraps an `McpToolset`, appending a `save_resource_as_artifact` tool.

    The runner holds this wrapper and calls its `get_tools_with_prefix`.
    The wrapper carries NO prefix of its own; instead `get_tools` returns
    the inner toolset's already-prefixed tools (via the inner's own
    `get_tools_with_prefix`) plus the pre-prefixed save tool from
    `bind_save_tool`. So every tool ends up with exactly one
    `mcp__<server>__` prefix and the inner toolset is left untouched.
    """

    def __init__(self, inner: McpToolset, server_name: str) -> None:
        super().__init__()
        self._inner = inner
        self._server_name = server_name

    async def get_tools(
        self, readonly_context: Optional[ReadonlyContext] = None
    ) -> list[BaseTool]:
        tools = list(await self._inner.get_tools_with_prefix(readonly_context))
        tools.append(bind_save_tool(self._inner, self._server_name))
        return tools

    async def close(self) -> None:
        await self._inner.close()
