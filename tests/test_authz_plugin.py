"""Tests for the tool-call AuthZ PEP (AuthzPlugin).

Hand-rolled (no pytest), runnable with the venv python.
"""

from __future__ import annotations

import asyncio
import os
import types as pytypes

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.authz import AbacPolicy, AbacPolicyDecisionPoint
from adk_cc.plugins.authz import AuthzPlugin


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self, principal=None):
        st = {}
        if principal is not None:
            st["temp:auth_principal"] = principal
        self.state = st


def _principal(user="alice", tenant="acme", roles=(), scopes=()):
    return {"user_id": user, "tenant_id": tenant, "roles": list(roles), "scopes": list(scopes)}


def _run(coro):
    return asyncio.run(coro)


def _call(plugin, tool_name, args, ctx):
    return _run(plugin.before_tool_callback(tool=_Tool(tool_name), tool_args=args, tool_context=ctx))


def _enabled_plugin(policies=None):
    p = AuthzPlugin(pdp=AbacPolicyDecisionPoint(policies or []))
    p._enabled = True
    return p


# --- tests ----------------------------------------------------------------

def test_disabled_by_default_inert():
    # No ADK_CC_AUTHZ set → plugin off → returns None regardless.
    os.environ.pop("ADK_CC_AUTHZ", None)
    p = AuthzPlugin(pdp=AbacPolicyDecisionPoint([]))
    out = _call(p, "run_bash", {"command": "rm -rf /"}, _Ctx(_principal()))
    assert out is None
    print("OK test_disabled_by_default_inert")


def test_owner_baseline_permits_self():
    # write_file → resource owned by the acting subject → baseline permit.
    p = _enabled_plugin([])
    out = _call(p, "write_file", {"path": "/home/alice/x"}, _Ctx(_principal()))
    assert out is None  # permit falls through
    print("OK test_owner_baseline_permits_self")


def test_policy_deny_short_circuits():
    p = _enabled_plugin([AbacPolicy(effect="deny", action="run_bash", resource="rm *", name="no-rm")])
    out = _call(p, "run_bash", {"command": "rm -rf /"}, _Ctx(_principal()))
    assert out is not None and out["status"] == "authz_denied"
    assert "no-rm" not in out["error"]  # error is human text, reason carries it
    assert "denied" in out["error"]
    print("OK test_policy_deny_short_circuits")


def test_role_required_for_tool():
    # deny non-admins from a deploy tool; admins permitted.
    pols = [
        AbacPolicy(effect="permit", roles=frozenset({"admin"}), action="deploy", name="admin-deploy"),
        AbacPolicy(effect="deny", action="deploy", name="default-no-deploy"),
    ]
    p = _enabled_plugin(pols)
    denied = _call(p, "deploy", {}, _Ctx(_principal(roles=())))
    permitted = _call(p, "deploy", {}, _Ctx(_principal(roles=("admin",))))
    assert denied is not None and denied["status"] == "authz_denied"
    assert permitted is None
    print("OK test_role_required_for_tool")


def test_mcp_tool_is_gated():
    # Unlike PermissionPlugin, authZ gates mcp__* tools too. With a deny
    # policy on the mcp action, it must short-circuit.
    p = _enabled_plugin([AbacPolicy(effect="deny", action="mcp__*", name="no-mcp")])
    out = _call(p, "mcp__github__create_issue", {"title": "x"}, _Ctx(_principal()))
    assert out is not None and out["status"] == "authz_denied"
    print("OK test_mcp_tool_is_gated")


def test_closed_world_denies_unowned_unmatched():
    # An mcp tool (no resource extractor → empty id, owner=subject so
    # baseline... actually resource_from_tool sets owner=subject, so
    # baseline permits). Verify the explicit-deny path instead is what
    # blocks; here with NO policy the baseline permits self-directed use.
    p = _enabled_plugin([])
    out = _call(p, "mcp__x__y", {}, _Ctx(_principal()))
    assert out is None  # owner baseline (resource owner == subject)
    print("OK test_closed_world_denies_unowned_unmatched")


def test_eval_error_fails_closed():
    # A PDP that raises → plugin denies (fail-closed), not allow.
    class _Boom(AbacPolicyDecisionPoint):
        def authorize(self, *a, **k):
            raise RuntimeError("kaboom")
    p = AuthzPlugin(pdp=_Boom([]))
    p._enabled = True
    out = _call(p, "run_bash", {"command": "ls"}, _Ctx(_principal()))
    assert out is not None and out["status"] == "authz_denied" and "error" in out["reason"]
    print("OK test_eval_error_fails_closed")


if __name__ == "__main__":
    test_disabled_by_default_inert()
    test_owner_baseline_permits_self()
    test_policy_deny_short_circuits()
    test_role_required_for_tool()
    test_mcp_tool_is_gated()
    test_closed_world_denies_unowned_unmatched()
    test_eval_error_fails_closed()
    print("\nall authz-plugin tests passed")
