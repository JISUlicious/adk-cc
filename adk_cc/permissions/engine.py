"""The permission decision flow.

A 4-step decision (collapsed from upstream's 7 — drop classifier path,
drop separate user-interaction phase, drop shadowed-rule diagnostics):

  1. If any rule matches with behavior=DENY → deny.
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

from typing import Literal, Optional

from pydantic import BaseModel

from ..tools.base import AdkCcTool
from .modes import PermissionMode
from .rules import PermissionRule, RuleBehavior, rule_matches
from .settings import SettingsHierarchy


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
