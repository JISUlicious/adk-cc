"""Policy Enforcement Points (PEPs) for the authZ layer.

Two in-loop gates, both subject-aware and both running BEFORE the
confirmation layer:

  - `before_tool_callback` — gates every tool call (incl. `mcp__*` and
    sub-agent-invoked tools; it does NOT inherit PermissionPlugin's
    `isinstance(AdkCcTool)` skip). Builds subject + action + resource and
    asks the PDP, additionally injecting the tool's CAPABILITY REQUIREMENT
    (ToolMeta.required_permissions ∪ matching YAML `requirements:`) so the
    PDP's requirement gate can enforce "only holders of permission X may
    use this tool."

  - `before_agent_callback` — gates SUB-AGENT invocation (e.g. the
    coordinator handing off to `Explore` / `verification`). Looks up the
    agent's capability requirement (the `AGENT_REQUIRED_PERMISSIONS`
    registry ∪ matching YAML `requirements:` with target agent) and denies
    the handoff (returns a `types.Content`, ADK's documented short-circuit)
    when the subject lacks it.

Distinct from PermissionPlugin (confirmation): this is hard, subject-aware
authorization, and it ignores permission mode (so a deny holds under
`bypassPermissions`).

Default-OFF: inert unless `ADK_CC_AUTHZ=1`. When enabled the PDP is
closed-world (unmatched ⇒ deny), so operators MUST grant tool access via
`policies:` (or rely on the ownership/tenant baseline, which permits a
subject's self-directed tool use by default). The capability requirement
gate is additionally inert per-action unless that action declares a
requirement.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..authz import (
    AbacPolicyDecisionPoint,
    Action,
    AuthzContext,
    DeclaredRequirementProvider,
    PolicyDecisionPoint,
    RequirementProvider,
    RequirementResolver,
    Resource,
    resource_from_tool,
    subject_from_state,
)

_log = logging.getLogger(__name__)


class AuthzPlugin(BasePlugin):
    """Hard subject×action×resource authorization gate on tools + agents."""

    def __init__(
        self,
        *,
        pdp: Optional[PolicyDecisionPoint] = None,
        resolver: Optional[RequirementResolver] = None,
        agent_requirements: Optional[dict[str, frozenset[str]]] = None,
        requirement_provider: Optional[RequirementProvider] = None,
        name: str = "adk_cc_authz",
    ) -> None:
        super().__init__(name=name)
        self._enabled = os.environ.get("ADK_CC_AUTHZ") == "1"
        # PDP is injectable (tests / external engines); default is the
        # ABAC PDP loaded from the same YAML the permission layer uses.
        self._pdp = pdp if pdp is not None else _default_pdp()
        # The requirement source is the RequirementProvider seam (what a
        # tool/agent demands). When one is injected, it wins; otherwise we
        # build the DEFAULT DeclaredRequirementProvider from the resolver
        # (YAML `requirements:` ∪ ToolMeta) + the agent registry — identical
        # to the prior behavior. A custom provider (e.g. the grant-header
        # scheme) can template requirements with the invoking agent.
        if requirement_provider is not None:
            self._provider = requirement_provider
        else:
            self._provider = _default_provider(resolver, agent_requirements)

    # -- tool-call PEP --------------------------------------------------

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
            # Effective capability requirement from the provider seam. The
            # invoking agent (ToolContext.agent_name — populated from the
            # live invocation context) is passed so a provider can scope the
            # tool requirement per agent (e.g. svc:{agent}:func:{tool}).
            required = self._provider.for_tool(
                tool_name,
                tool_meta=getattr(tool, "meta", None),
                invoking_agent=getattr(tool_context, "agent_name", None),
            )
            decision = self._pdp.authorize(
                subject,
                Action(tool_name),
                resource,
                AuthzContext(mode=mode, required_permissions=required),
            )
        except Exception as e:  # noqa: BLE001 — fail CLOSED on authZ errors
            _log.warning("authz: evaluation error for %s: %s", tool_name, e)
            reason = f"authz evaluation error: {e}"
            _emit(tool_name, tool_args, "deny", reason, None)
            return {
                "status": "authz_denied",
                "error": f"Tool {tool_name!r} denied: {reason}",
                "reason": reason,
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

    # -- sub-agent PEP --------------------------------------------------

    async def before_agent_callback(
        self,
        *,
        agent,  # noqa: ANN001 — BaseAgent, typed by ADK
        callback_context,  # noqa: ANN001 — CallbackContext, typed by ADK
    ):
        """Gate (sub-)agent invocation by capability.

        Returns a `types.Content` to DENY the handoff (ADK short-circuits
        the agent and surfaces this content), or None to allow. Default-OFF
        and inert unless the agent declares a requirement, so the root
        coordinator (no requirement) is never blocked.
        """
        if not self._enabled:
            return None

        agent_name = getattr(agent, "name", "") or ""
        try:
            required = self._provider.for_agent(agent_name)
            # No requirement for this agent → nothing to enforce. Skip even
            # building the subject so the ungated path stays cheap and the
            # root coordinator (no requirement) is never blocked.
            if not required:
                return None
            state = getattr(callback_context, "state", None)
            subject = subject_from_state(state if state is not None else {})
            # Route through the SAME PDP as tools — do NOT do an inline
            # subset check here, or a swapped-in custom PDP (OPA/Cerbos)
            # would govern tools but silently NOT agents. The agent is the
            # resource, owned by the subject, so a met requirement falls
            # through to the ownership baseline → permit (mirrors tools).
            resource = Resource(
                type="agent",
                id=agent_name,
                owner_user_id=subject.user_id,
                tenant_id=subject.tenant_id,
                attrs={"agent": agent_name},
            )
            decision = self._pdp.authorize(
                subject,
                Action(f"invoke_agent:{agent_name}"),
                resource,
                AuthzContext(required_permissions=required),
            )
        except Exception as e:  # noqa: BLE001 — fail CLOSED on authZ errors
            _log.warning("authz: agent eval error for %s: %s", agent_name, e)
            _emit(f"agent:{agent_name}", {}, "deny", f"agent authz error: {e}", None)
            return _deny_content(
                f"Access to agent {agent_name!r} denied (authorization error)."
            )

        _emit(f"agent:{agent_name}", {}, decision.effect, decision.reason, decision.matched)
        if decision.effect == "deny":
            return _deny_content(
                f"Access to agent {agent_name!r} denied by authorization policy."
            )
        return None


def _deny_content(message: str):
    """Build the ADK short-circuit Content for an agent-handoff denial."""
    from google.genai import types

    return types.Content(role="model", parts=[types.Part(text=message)])


def _default_provider(
    resolver: Optional[RequirementResolver],
    agent_requirements: Optional[dict[str, frozenset[str]]],
) -> RequirementProvider:
    """Pick the default requirement provider from the environment.

    `ADK_CC_GRANT_HEADER=1` selects the gateway presence scheme (the
    gateway is authoritative; requirement = presence in the grant). Otherwise
    the declared scheme (ToolMeta ∪ YAML `requirements:` + agent registry).
    An explicitly injected provider/resolver always takes precedence over
    this (handled by the caller)."""
    if resolver is None:
        try:
            from ..service.grant_header_auth import grant_provider_from_env

            grant = grant_provider_from_env()
            if grant is not None:
                return grant
        except Exception as e:  # noqa: BLE001 — never block boot on the adapter
            _log.error("authz: grant provider init failed: %s", e)
    resolver = resolver if resolver is not None else _default_resolver()
    return DeclaredRequirementProvider(resolver, agent_requirements)


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


def _default_resolver() -> RequirementResolver:
    """Build the requirement resolver from the YAML `requirements:` block."""
    path = os.environ.get("ADK_CC_PERMISSIONS_YAML")
    reqs = []
    if path:
        try:
            from ..authz.policy_loader import load_requirements_from_yaml

            reqs = load_requirements_from_yaml(path)
        except Exception as e:  # noqa: BLE001 — bad file shouldn't crash boot
            _log.error("authz: failed to load requirements from %s: %s", path, e)
    return RequirementResolver(reqs)


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
