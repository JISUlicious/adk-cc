"""Cross-layer ordering lock: bypassPermissions × guardrail-deny × authz-deny.

Locks the two-layer security contract so a future refactor can't silently
weaken it:

  - PermissionPlugin owns CONFIRMATION + mode + a subject-blind guardrail
    deny-list. `bypassPermissions` skips the confirmation prompt — but a
    guardrail DENY rule still denies (the engine evaluates DENY *before*
    the bypass short-circuit). "Is this operation forbidden for anyone?"
  - `dontAsk` is a DISTINCT mode (NOT an alias of bypass): it converts
    every ask to deny.
  - AuthzPlugin owns subject-aware AUTHORIZATION. It runs BEFORE
    PermissionPlugin and ignores mode entirely, so an authz DENY holds
    under bypassPermissions too. "May THIS subject act on THIS resource?"

Hand-rolled (no pytest), run with the venv python.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, ClassVar

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from pydantic import BaseModel

from adk_cc.authz import AbacPolicy, AbacPolicyDecisionPoint
from adk_cc.permissions.modes import PermissionMode
from adk_cc.permissions.rules import PermissionRule, RuleBehavior, RuleSource
from adk_cc.permissions.settings import SettingsHierarchy
from adk_cc.plugins.authz import AuthzPlugin
from adk_cc.plugins.permissions import PermissionPlugin
from adk_cc.tools.base import AdkCcTool, ToolMeta


class _Args(BaseModel):
    command: str = ""


class _StubTool(AdkCcTool):
    """A destructive run_bash stand-in (mirrors the ClassVar-meta pattern
    used by the other permission tests)."""

    meta: ClassVar[ToolMeta] = ToolMeta(
        name="run_bash",
        is_read_only=False,
        is_concurrency_safe=False,
        is_destructive=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = "stub bash"

    async def _execute(self, args: BaseModel, ctx: Any) -> dict:
        return {"ok": True}


class _FakeActions:
    def __init__(self):
        self.skip_summarization = False


class _FakeToolContext:
    def __init__(self, *, state=None, function_call_id="fc-1"):
        self.state = state or {}
        self.function_call_id = function_call_id
        self.tool_confirmation = None
        self.actions = _FakeActions()


def _run(coro):
    return asyncio.run(coro)


def _deny_rule(content="rm *"):
    return PermissionRule(
        source=RuleSource.POLICY,
        behavior=RuleBehavior.DENY,
        tool_name="run_bash",
        rule_content=content,
    )


# --- PermissionPlugin: guardrail deny vs bypass ---------------------------

def test_guardrail_deny_survives_bypass():
    """A guardrail DENY rule denies even under bypassPermissions — the
    engine evaluates DENY before the bypass short-circuit."""
    plugin = PermissionPlugin(
        SettingsHierarchy([_deny_rule()]),
        default_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    ctx = _FakeToolContext()
    out = _run(
        plugin.before_tool_callback(
            tool=_StubTool(), tool_args={"command": "rm -rf /"}, tool_context=ctx
        )
    )
    assert out is not None and out["status"] == "permission_denied", out
    print("OK test_guardrail_deny_survives_bypass")


def test_bypass_skips_confirmation():
    """A destructive tool with no rule ASKs in DEFAULT mode; under
    bypassPermissions it runs (confirmation skipped)."""
    plugin = PermissionPlugin(
        SettingsHierarchy([]),
        default_mode=PermissionMode.BYPASS_PERMISSIONS,
    )
    ctx = _FakeToolContext()
    out = _run(
        plugin.before_tool_callback(
            tool=_StubTool(), tool_args={"command": "ls"}, tool_context=ctx
        )
    )
    assert out is None, out  # allowed, no needs_confirmation
    print("OK test_bypass_skips_confirmation")


def test_dontask_is_distinct_and_denies_ask():
    """dontAsk must NOT be bypass: it converts a destructive ask to deny."""
    assert PermissionMode.DONT_ASK is not PermissionMode.BYPASS_PERMISSIONS
    assert PermissionMode.DONT_ASK.value == "dontAsk"
    plugin = PermissionPlugin(
        SettingsHierarchy([]),
        default_mode=PermissionMode.DONT_ASK,
    )
    ctx = _FakeToolContext()
    out = _run(
        plugin.before_tool_callback(
            tool=_StubTool(), tool_args={"command": "ls"}, tool_context=ctx
        )
    )
    assert out is not None and out["status"] == "permission_denied", out
    print("OK test_dontask_is_distinct_and_denies_ask")


# --- AuthzPlugin: authorization deny vs bypass ----------------------------

def _principal(user="alice", tenant="acme", roles=(), scopes=()):
    return {
        "user_id": user,
        "tenant_id": tenant,
        "roles": list(roles),
        "scopes": list(scopes),
    }


class _AzCtx:
    def __init__(self, principal, mode=None):
        st = {"temp:auth_principal": principal}
        if mode is not None:
            st["permission_mode"] = mode
        self.state = st


def test_authz_deny_survives_bypass():
    """AuthzPlugin runs before PermissionPlugin and ignores mode — an authz
    DENY holds even when permission_mode is bypassPermissions."""
    p = AuthzPlugin(
        pdp=AbacPolicyDecisionPoint(
            [AbacPolicy(effect="deny", action="run_bash", name="no-bash")]
        )
    )
    p._enabled = True
    ctx = _AzCtx(_principal(), mode="bypassPermissions")
    out = _run(
        p.before_tool_callback(
            tool=_StubTool(), tool_args={"command": "ls"}, tool_context=ctx
        )
    )
    assert out is not None and out["status"] == "authz_denied", out
    print("OK test_authz_deny_survives_bypass")


if __name__ == "__main__":
    test_guardrail_deny_survives_bypass()
    test_bypass_skips_confirmation()
    test_dontask_is_distinct_and_denies_ask()
    test_authz_deny_survives_bypass()
    print("\nall layering tests passed")
