"""The permission decision flow.

Scope: this is the CONFIRMATION layer's decision function. Its DENY step is
a subject-BLIND guardrail blocklist ("is this operation forbidden for
*anyone*?"), NOT authorization ("may *this subject* do it?"). Subject-aware
authorization lives in `authz/pdp.py` + `plugins/authz.py`, which gate the
call earlier and independently. See `plugins/permissions.py` for the full
layer-boundary note.

A 4-step decision (collapsed from upstream's 7 — drop classifier path,
drop separate user-interaction phase, drop shadowed-rule diagnostics):

  1. If any rule matches with behavior=DENY → deny. (Guardrail blocklist —
     subject-blind; evaluated BEFORE the bypass short-circuit in step 2b so
     bypassPermissions cannot skip it.)
  2. Mode override:
        - PLAN mode blocks any tool whose meta marks it as not read-only.
        - DONT_ASK mode converts every ask outcome (rule-based or destructive
          fallback) to deny.
        - BYPASS_PERMISSIONS mode skips the remaining rule checks (deny
          already applied above is the only gate).
  3. If any rule matches with behavior=ASK → ask, unless BYPASS_PERMISSIONS
     or an earlier-evaluated ALLOW rule matches.
  4. Tool-specific safety: if the tool's meta is_destructive=True and no
     explicit ALLOW rule matched, the result is `ask` in DEFAULT mode and
     `allow` in ACCEPT_EDITS mode (which auto-approves edits).

Otherwise → allow.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel

from ..tools.base import AdkCcTool
from .modes import PermissionMode
from .rules import PermissionRule, RuleBehavior, rule_matches
from .settings import SettingsHierarchy

# NOTE: `..plugins.audit.emit_permission_decision` is imported lazily
# inside `decide()` rather than at module-load. permissions/__init__.py
# imports engine.py, and plugins/__init__.py imports plugins.permissions
# which imports permissions.engine. Eagerly importing
# `..plugins.audit` here would trigger plugins/__init__.py, which would
# then re-enter permissions.engine before this module finishes loading,
# raising a circular-import error.

_log = logging.getLogger(__name__)


class PermissionDecision(BaseModel):
    behavior: Literal["allow", "deny", "ask"]
    matched_rule: Optional[PermissionRule] = None
    reason: str = ""


def _first_match(
    rules: list[PermissionRule],
    behavior: RuleBehavior,
    tool_name: str,
    args: dict,
) -> Optional[PermissionRule]:
    for r in rules:
        if r.behavior is behavior and rule_matches(r, tool_name, args):
            return r
    return None


def decide(
    *,
    tool: AdkCcTool,
    args: dict,
    mode: PermissionMode,
    settings: SettingsHierarchy,
) -> PermissionDecision:
    decision = _decide_impl(tool=tool, args=args, mode=mode, settings=settings)
    matched_rule_dump = (
        decision.matched_rule.model_dump() if decision.matched_rule else None
    )
    # DEBUG log every decision so operators can trace *why* a tool was
    # denied/asked. The matched_rule (if any) carries the rule_content
    # pattern that matched — load-bearing when debugging "why did my
    # Allow always rule not fire?".
    if _log.isEnabledFor(logging.DEBUG):
        _log.debug(
            "decide tool=%s behavior=%s reason=%s rule=%s mode=%s args=%s",
            tool.meta.name,
            decision.behavior,
            decision.reason,
            matched_rule_dump,
            mode.value,
            args,
            extra={
                "tool_name": tool.meta.name,
                "behavior": decision.behavior,
                "reason": decision.reason,
                "matched_rule": matched_rule_dump,
                "mode": mode.value,
            },
        )
    # Also emit a structured audit event when audit is configured —
    # durable record of every decision for post-hoc analysis. No-op
    # when no AuditPlugin is registered. Deferred import — see module
    # docstring NOTE.
    from ..plugins.audit import emit_permission_decision
    emit_permission_decision(
        tool_name=tool.meta.name,
        args=args,
        behavior=decision.behavior,
        reason=decision.reason,
        matched_rule=matched_rule_dump,
        mode=mode.value,
    )
    return decision


def _decide_impl(
    *,
    tool: AdkCcTool,
    args: dict,
    mode: PermissionMode,
    settings: SettingsHierarchy,
) -> PermissionDecision:
    rules = settings.all_rules()
    tool_name = tool.meta.name

    # Step 1: deny rules always win.
    deny = _first_match(rules, RuleBehavior.DENY, tool_name, args)
    if deny is not None:
        return PermissionDecision(
            behavior="deny",
            matched_rule=deny,
            reason=f"denied by {deny.source.value} rule",
        )

    # Step 2a: PLAN mode blocks every non-read-only tool.
    if mode is PermissionMode.PLAN and not tool.meta.is_read_only:
        return PermissionDecision(
            behavior="deny",
            reason=f"{tool_name} is blocked in plan mode",
        )

    # Step 2b: BYPASS skips the rest (the only gate is the deny check above).
    if mode is PermissionMode.BYPASS_PERMISSIONS:
        return PermissionDecision(
            behavior="allow", reason="bypassPermissions mode"
        )

    # Pre-compute allow match — used to short-circuit the ask path and the
    # destructive-tool fallback.
    allow = _first_match(rules, RuleBehavior.ALLOW, tool_name, args)

    # Step 3: ask rules (ALLOW takes precedence if present).
    ask = _first_match(rules, RuleBehavior.ASK, tool_name, args)
    if ask is not None and allow is None:
        if mode is PermissionMode.DONT_ASK:
            return PermissionDecision(
                behavior="deny",
                matched_rule=ask,
                reason=f"ask rule converted to deny in dontAsk mode",
            )
        return PermissionDecision(
            behavior="ask",
            matched_rule=ask,
            reason=f"requires confirmation per {ask.source.value} rule",
        )

    # Step 4: destructive-tool fallback.
    if tool.meta.is_destructive and allow is None:
        if mode is PermissionMode.ACCEPT_EDITS:
            return PermissionDecision(
                behavior="allow", reason="acceptEdits mode auto-approves"
            )
        if mode is PermissionMode.DONT_ASK:
            return PermissionDecision(
                behavior="deny",
                reason=f"destructive {tool_name} blocked in dontAsk mode",
            )
        return PermissionDecision(
            behavior="ask",
            reason=f"destructive {tool_name} requires confirmation",
        )

    return PermissionDecision(
        behavior="allow",
        matched_rule=allow,
        reason="no matching rule, allowed by default" if allow is None
        else f"allowed by {allow.source.value} rule",
    )
