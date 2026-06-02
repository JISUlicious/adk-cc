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

from ..credentials import CredentialProvider
from ..service.registry import TenantResourceRegistry
# McpServerConfig now lives in tools/mcp.py (the shared base, so the static
# multi-server loader can reuse it without a circular import). Re-exported
# here so existing `from ...tools.mcp_tenant import McpServerConfig` imports
# (agent.py, admin_routes.py, tests, docs) keep working unchanged.
from .mcp import McpServerConfig, bind_save_tool, connection_params_for

_log = logging.getLogger(__name__)

__all__ = ["McpServerConfig", "TenantMcpToolset"]


def _build_connection_params(cfg: McpServerConfig, secret: Optional[str]):
    """Translate `(McpServerConfig, secret)` → ADK MCP connection params.

    Delegates the transport→params mapping to `connection_params_for`
    (shared with the static wiring). Secrets land in the
    `Authorization: Bearer ...` header for HTTP-based transports, which is
    the common shape; operators with non-bearer auth write a custom
    toolset.
    """
    headers = {"Authorization": f"Bearer {secret}"} if secret else None
    return connection_params_for(cfg.transport, cfg.url, headers=headers)


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
                    use_mcp_resources=cfg.use_mcp_resources,
                )
                tools = await inner.get_tools_with_prefix(readonly_context)
                out.extend(tools)
                if cfg.save_resources_as_artifacts:
                    # Appended AFTER the inner tools were already prefixed,
                    # so bind_save_tool returns it with a matching
                    # mcp__{server}___ name (shared with the static wiring).
                    out.append(bind_save_tool(inner, cfg.server_name))
            except Exception as e:  # noqa: BLE001 — one bad MCP shouldn't kill the rest
                _log.warning(
                    "TenantMcpToolset: skipping server %r for tenant %r: %s",
                    cfg.server_name,
                    tenant_id,
                    e,
                )
        return out
