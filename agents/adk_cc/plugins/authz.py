"""Tool-call Policy Enforcement Point (PEP).

The in-loop authZ gate: before any tool runs, build the subject (from the
authenticated principal seeded in session state), the action (tool name)
and the resource (tool args), ask the PDP, and DENY by returning a dict
(ADK's before_tool_callback short-circuit) or fall through (None) to the
confirmation layer.

Distinct from PermissionPlugin (confirmation): this is hard, subject-aware
authorization. It runs BEFORE PermissionPlugin and gates ALL tools —
including `mcp__*` and sub-agent-invoked tools (it does NOT inherit the
`isinstance(AdkCcTool)` skip).

Default-OFF: inert unless `ADK_CC_AUTHZ=1`. When enabled the PDP is
closed-world (unmatched ⇒ deny), so operators MUST grant tool access via
`policies:` (or rely on the ownership/tenant baseline, which permits a
subject's self-directed tool use by default).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..config.schema import env_bool
from ..authz import (
    AbacPolicyDecisionPoint,
    Action,
    AuthzContext,
    PolicyDecisionPoint,
    resource_from_tool,
    subject_from_state,
)

_log = logging.getLogger(__name__)


class AuthzPlugin(BasePlugin):
    """Hard subject×action×resource authorization gate on tool calls."""

    def __init__(
        self,
        *,
        pdp: Optional[PolicyDecisionPoint] = None,
        name: str = "adk_cc_authz",
    ) -> None:
        super().__init__(name=name)
        self._enabled = env_bool("ADK_CC_AUTHZ")
        # PDP is injectable (tests / external engines); default is the
        # ABAC PDP loaded from the same YAML the permission layer uses.
        self._pdp = pdp if pdp is not None else _default_pdp()

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        if not self._enabled:
            return None

        tool_name = getattr(tool, "name", None) or getattr(
            getattr(tool, "meta", None), "name", ""
        )
        try:
            subject = subject_from_state(tool_context.state)
            resource = resource_from_tool(tool_name, tool_args, subject)
            mode = _safe_mode(tool_context)
            decision = self._pdp.authorize(
                subject,
                Action(tool_name),
                resource,
                AuthzContext(mode=mode),
            )
        except Exception as e:  # noqa: BLE001 — fail CLOSED on authZ errors
            _log.warning("authz: evaluation error for %s: %s", tool_name, e)
            decision_denied_reason = f"authz evaluation error: {e}"
            _emit(tool_name, tool_args, "deny", decision_denied_reason, None)
            return {
                "status": "authz_denied",
                "error": f"Tool {tool_name!r} denied: {decision_denied_reason}",
                "reason": decision_denied_reason,
            }

        _emit(tool_name, tool_args, decision.effect, decision.reason, decision.matched)

        if decision.effect == "deny":
            return {
                "status": "authz_denied",
                # `error` is the model-/user-visible message: clean human
                # text only — no internal policy identifiers. The detailed
                # PDP reason (which may name the matched policy) goes in
                # `reason`; the policy name is carried in `matched`.
                "error": f"Tool {tool_name!r} denied by authorization policy.",
                "reason": decision.reason,
                "matched": decision.matched,
            }
        return None  # permit → fall through to PermissionPlugin (confirmation)


def _default_pdp() -> PolicyDecisionPoint:
    """Build the default ABAC PDP from ADK_CC_PERMISSIONS_YAML (policies:
    block). Empty policy list when no file / no policies — the baseline
    (owner/tenant) still applies."""
    path = os.environ.get("ADK_CC_PERMISSIONS_YAML")
    policies = []
    if path:
        try:
            from ..authz.policy_loader import load_policies_from_yaml

            policies = load_policies_from_yaml(path)
        except Exception as e:  # noqa: BLE001 — bad policy file shouldn't crash boot
            _log.error("authz: failed to load policies from %s: %s", path, e)
    return AbacPolicyDecisionPoint(policies)


def _safe_mode(tool_context: ToolContext) -> Optional[str]:
    try:
        m = tool_context.state.get("permission_mode")
        return str(m) if m is not None else None
    except Exception:  # noqa: BLE001
        return None


def _emit(
    tool_name: str,
    args: dict,
    effect: str,
    reason: str,
    matched: Optional[str],
) -> None:
    """Audit every authZ decision (reuses the permission audit channel)."""
    try:
        from .audit import emit_permission_decision

        emit_permission_decision(
            tool_name=tool_name,
            args=args,
            behavior=effect,  # "permit"/"deny"
            reason=f"authz: {reason}",
            matched_rule=matched,
            mode=None,
        )
    except Exception:  # noqa: BLE001 — audit must never break the gate
        pass
