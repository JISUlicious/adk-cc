"""Per-tenant quota plugin.

Counts tool calls per tenant per window and denies new calls when the
tenant is over budget. Runs after the permission plugin so denied calls
don't consume quota; runs before audit's after-callback so quota state
is observed.

In v1 the counter is in-process. For multi-process deployments swap the
counter for a Redis or DB-backed implementation that keys on the same
(tenant_id, window) pair.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext


class QuotaPlugin(BasePlugin):
    def __init__(
        self,
        *,
        calls_per_minute: int = 120,
        name: str = "adk_cc_quotas",
    ) -> None:
        super().__init__(name=name)
        self._limit = calls_per_minute
        # tenant_id -> (window_start_seconds, count_in_window)
        self._counters: dict[str, tuple[int, int]] = {}

    def _tenant_id(self, ctx: ToolContext) -> Optional[str]:
        # Lazy import: importing service.tenancy at module level loops
        # (service.__init__ → server → plugins → quotas → service.tenancy).
        from ..service.tenancy import _STATE_TENANT_KEY

        try:
            tenant = ctx.state.get(_STATE_TENANT_KEY)
        except Exception:
            return None
        if tenant is None:
            return None
        return getattr(tenant, "tenant_id", None)

    def _charge(self, tenant_id: str) -> bool:
        """True if the call is within budget; False if over."""
        now_window = int(time.time() // 60)
        window, count = self._counters.get(tenant_id, (now_window, 0))
        if window != now_window:
            window, count = now_window, 0
        if count >= self._limit:
            self._counters[tenant_id] = (window, count)
            return False
        self._counters[tenant_id] = (window, count + 1)
        return True

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        tenant_id = self._tenant_id(tool_context)
        if tenant_id is None:
            # No tenant attached → we're in dev or a misconfigured deploy;
            # don't enforce a quota we can't attribute.
            return None
        if not self._charge(tenant_id):
            return {
                "status": "quota_exceeded",
                "reason": (
                    f"tenant {tenant_id!r} exceeded {self._limit} tool calls/min"
                ),
            }
        return None
