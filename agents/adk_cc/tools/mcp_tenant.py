"""Tenant-scoped MCP toolset (subclass of ADK's `BaseToolset`).

Resolves per-invocation: ADK calls `get_tools(readonly_context)` at the
start of each invocation, this class reads `tenant_id` from the
context's session state, looks up the tenant's registered MCP servers
from a `TenantResourceRegistry[McpServerConfig]`, fetches credentials
from a `CredentialProvider`, builds the inner `McpToolset` instances,
and returns the union of their tools.

Hot reload comes for free: each invocation re-reads the registry, so
adding / removing an MCP server takes effect on the next session
without restarting the agent process.

The static `make_mcp_toolset` factory in `tools/mcp.py` stays for
single-tenant deployments wiring static MCPs at boot. Use this class
for the multi-tenant SaaS shape.
"""

from __future__ import annotations

import logging
from typing import Optional

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.mcp_tool import McpToolset
from pydantic import BaseModel, Field

from ..credentials import CredentialProvider
from ..service.registry import TenantResourceRegistry

_log = logging.getLogger(__name__)


class McpServerConfig(BaseModel):
    """Per-tenant MCP server config persisted in the registry."""

    server_name: str = Field(description="Logical name; also the tool prefix")
    transport: str = Field(description="One of: 'sse', 'http', 'stdio'")
    url: str = Field(description="Server URL or stdio command")
    credential_key: Optional[str] = Field(
        default=None,
        description=(
            "Optional CredentialProvider key whose value is substituted "
            "into the connection's auth header. None = unauthenticated."
        ),
    )
    tool_filter: Optional[list[str]] = Field(
        default=None,
        description="Optional subset of tool names to expose; None = all.",
    )
    require_confirmation: bool = Field(
        default=False,
        description="If True, every MCP call goes through ADK's request_confirmation flow.",
    )


def _build_connection_params(cfg: McpServerConfig, secret: Optional[str]):
    """Translate `(McpServerConfig, secret)` → ADK MCP connection params.

    ADK ships three transport-specific param classes; we pick by
    `cfg.transport`. Secrets land in the `Authorization: Bearer ...`
    header for HTTP-based transports, which is the common shape.
    Operators with non-bearer auth (e.g. API key in a custom header)
    write a custom toolset.
    """
    headers = {"Authorization": f"Bearer {secret}"} if secret else None

    if cfg.transport == "sse":
        from google.adk.tools.mcp_tool.mcp_session_manager import SseConnectionParams

        return SseConnectionParams(url=cfg.url, headers=headers)
    if cfg.transport == "http":
        from google.adk.tools.mcp_tool.mcp_session_manager import (
            StreamableHTTPConnectionParams,
        )

        return StreamableHTTPConnectionParams(url=cfg.url, headers=headers)
    if cfg.transport == "stdio":
        from mcp import StdioServerParameters

        # cfg.url for stdio is the command line; we tokenize naively.
        # Operators wanting full control over stdio args write a
        # custom toolset.
        import shlex

        parts = shlex.split(cfg.url)
        return StdioServerParameters(command=parts[0], args=parts[1:])
    raise ValueError(f"unknown MCP transport: {cfg.transport!r}")


class TenantMcpToolset(BaseToolset):
    """`BaseToolset` impl that resolves MCP servers per-tenant per-invocation.

    Wire into `LlmAgent.tools=[...]`; ADK calls `get_tools` per invocation
    via `BaseToolset.get_tools_with_prefix` and the tools list is merged
    with the agent's static tools.
    """

    def __init__(
        self,
        *,
        registry: TenantResourceRegistry[McpServerConfig],
        credentials: CredentialProvider,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._credentials = credentials

    async def get_tools(
        self, readonly_context: Optional[ReadonlyContext] = None
    ) -> list[BaseTool]:
        if readonly_context is None:
            return []

        # tenant_id lives in session state, seeded by TenancyPlugin.
        try:
            state = readonly_context.session.state
            tenant = state.get("temp:tenant_context")
            tenant_id = (
                tenant.tenant_id
                if tenant is not None and hasattr(tenant, "tenant_id")
                else None
            )
        except Exception:
            tenant_id = None
        if not tenant_id:
            return []

        configs = await self._registry.list_for_tenant(tenant_id)
        out: list[BaseTool] = []
        for cfg in configs:
            try:
                secret = (
                    await self._credentials.get(
                        tenant_id=tenant_id, key=cfg.credential_key
                    )
                    if cfg.credential_key
                    else None
                )
                params = _build_connection_params(cfg, secret)
                inner = McpToolset(
                    connection_params=params,
                    tool_filter=cfg.tool_filter,
                    tool_name_prefix=f"mcp__{cfg.server_name}__",
                    require_confirmation=cfg.require_confirmation,
                )
                tools = await inner.get_tools_with_prefix(readonly_context)
                out.extend(tools)
            except Exception as e:  # noqa: BLE001 — one bad MCP shouldn't kill the rest
                _log.warning(
                    "TenantMcpToolset: skipping server %r for tenant %r: %s",
                    cfg.server_name,
                    tenant_id,
                    e,
                )
        return out
