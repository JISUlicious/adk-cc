"""Tests for capability/permission-based gating of tools and sub-agents.

Covers the four moving parts of the capability layer:
  1. PDP requirement gate — subject must hold ALL required permissions
     (AND); explicit PERMIT overrides; explicit DENY still wins; no
     requirement → baseline unchanged.
  2. RequirementResolver — code base ∪ YAML (augment), replace, target
     filtering (tool vs agent).
  3. Tool PEP — lacking the tool's required permission → authz_denied;
     holding → falls through; ungated tool unaffected.
  4. Agent PEP — handoff denied when subject lacks the agent's permission;
     allowed when held or when the agent declares no requirement.
  5. Auth extraction — bearer 4th segment + JWT permissions_claim +
     gateway-header merge (and ignored when not enabled).

Hand-rolled (no pytest), run with the venv python.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

# Imported at module scope so FastAPI's get_type_hints can resolve the
# `req: Request` annotation on the nested reflect route below.
from starlette.requests import Request  # noqa: E402,F401

from adk_cc.authz import (
    AbacPolicy,
    AbacPolicyDecisionPoint,
    Action,
    AuthzContext,
    PolicyDecisionPoint,
    Requirement,
    RequirementResolver,
    Resource,
    Subject,
)
from adk_cc.plugins.authz import AuthzPlugin


def _run(coro):
    return asyncio.run(coro)


# --- 1. PDP requirement gate ----------------------------------------------

def _subject(perms=(), user="alice", tenant="acme"):
    return Subject(user, tenant, permissions=frozenset(perms))


def _owned_resource(user="alice", tenant="acme"):
    # owner == subject so the ownership baseline WOULD permit absent a gate.
    return Resource(type="tool", id="", owner_user_id=user, tenant_id=tenant)


def test_pdp_requirement_met_permits():
    pdp = AbacPolicyDecisionPoint([])
    d = pdp.authorize(
        _subject(["tool:deploy"]),
        Action("deploy"),
        _owned_resource(),
        AuthzContext(required_permissions=frozenset({"tool:deploy"})),
    )
    assert d.effect == "permit", d
    print("OK test_pdp_requirement_met_permits")


def test_pdp_requirement_missing_denies_despite_ownership():
    # The whole point: ownership would permit, but the requirement gate
    # bites first because the subject lacks the capability.
    pdp = AbacPolicyDecisionPoint([])
    d = pdp.authorize(
        _subject([]),
        Action("deploy"),
        _owned_resource(),
        AuthzContext(required_permissions=frozenset({"tool:deploy"})),
    )
    assert d.effect == "deny" and d.matched == "requirement", d
    assert "tool:deploy" in d.reason
    print("OK test_pdp_requirement_missing_denies_despite_ownership")


def test_pdp_requirement_and_semantics():
    pdp = AbacPolicyDecisionPoint([])
    req = frozenset({"a", "b"})
    # holds only one → deny
    d1 = pdp.authorize(_subject(["a"]), Action("x"), _owned_resource(),
                       AuthzContext(required_permissions=req))
    # holds both → permit
    d2 = pdp.authorize(_subject(["a", "b"]), Action("x"), _owned_resource(),
                       AuthzContext(required_permissions=req))
    assert d1.effect == "deny" and "b" in d1.reason, d1
    assert d2.effect == "permit", d2
    print("OK test_pdp_requirement_and_semantics")


def test_pdp_explicit_permit_overrides_requirement():
    # An operator PERMIT policy is evaluated BEFORE the requirement gate,
    # so it can grant access to a subject lacking the capability.
    pdp = AbacPolicyDecisionPoint([
        AbacPolicy(effect="permit", roles=frozenset({"admin"}),
                   action="deploy", name="admin-grant"),
    ])
    s = Subject("alice", "acme", roles=frozenset({"admin"}), permissions=frozenset())
    d = pdp.authorize(s, Action("deploy"), _owned_resource(),
                     AuthzContext(required_permissions=frozenset({"tool:deploy"})))
    assert d.effect == "permit" and d.matched == "admin-grant", d
    print("OK test_pdp_explicit_permit_overrides_requirement")


def test_pdp_explicit_deny_still_wins_over_requirement():
    pdp = AbacPolicyDecisionPoint([
        AbacPolicy(effect="deny", action="deploy", name="no-deploy"),
    ])
    # Subject HOLDS the capability, but an explicit deny precedes the gate.
    d = pdp.authorize(_subject(["tool:deploy"]), Action("deploy"), _owned_resource(),
                     AuthzContext(required_permissions=frozenset({"tool:deploy"})))
    assert d.effect == "deny" and d.matched == "no-deploy", d
    print("OK test_pdp_explicit_deny_still_wins_over_requirement")


def test_pdp_no_requirement_baseline_unchanged():
    pdp = AbacPolicyDecisionPoint([])
    d = pdp.authorize(_subject([]), Action("read_file"), _owned_resource(),
                     AuthzContext())  # no required_permissions
    assert d.effect == "permit" and d.matched == "baseline:owner", d
    print("OK test_pdp_no_requirement_baseline_unchanged")


# --- 2. RequirementResolver -----------------------------------------------

def test_resolver_augment_replace_target():
    reqs = [
        Requirement(match="deploy", permissions=frozenset({"tool:deploy"}), target="tool"),
        Requirement(match="locked", permissions=frozenset({"only"}), target="tool", mode="replace"),
        Requirement(match="Explore", permissions=frozenset({"agent:explore"}), target="agent"),
    ]
    r = RequirementResolver(reqs)
    # augment unions onto code base
    assert r.resolve("deploy", target="tool", base=frozenset({"base"})) == frozenset({"base", "tool:deploy"})
    # replace discards base
    assert r.resolve("locked", target="tool", base=frozenset({"base"})) == frozenset({"only"})
    # target filtering: agent rule does not apply to a tool of same name
    assert r.resolve("Explore", target="tool") == frozenset()
    assert r.resolve("Explore", target="agent") == frozenset({"agent:explore"})
    # ungated
    assert r.resolve("read_file", target="tool") == frozenset()
    print("OK test_resolver_augment_replace_target")


# --- 3. Tool PEP ----------------------------------------------------------

class _Tool:
    def __init__(self, name, required=()):
        self.name = name
        self.meta = type("M", (), {"name": name, "required_permissions": frozenset(required)})()


class _Ctx:
    def __init__(self, perms=()):
        self.state = {"temp:auth_principal": {
            "user_id": "alice", "tenant_id": "acme",
            "roles": [], "scopes": [], "permissions": list(perms),
        }}


def _enabled_plugin(resolver=None, pdp=None):
    p = AuthzPlugin(
        pdp=pdp or AbacPolicyDecisionPoint([]),
        resolver=resolver or RequirementResolver([]),
        agent_requirements={},
    )
    p._enabled = True
    return p


def _call_tool(p, tool, ctx):
    return _run(p.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx))


def test_tool_pep_denies_when_lacking_meta_permission():
    p = _enabled_plugin()
    out = _call_tool(p, _Tool("deploy", required=["tool:deploy"]), _Ctx(perms=[]))
    assert out is not None and out["status"] == "authz_denied", out
    print("OK test_tool_pep_denies_when_lacking_meta_permission")


def test_tool_pep_permits_when_holding_meta_permission():
    p = _enabled_plugin()
    out = _call_tool(p, _Tool("deploy", required=["tool:deploy"]), _Ctx(perms=["tool:deploy"]))
    assert out is None, out
    print("OK test_tool_pep_permits_when_holding_meta_permission")


def test_tool_pep_yaml_requirement_gates_ungated_tool():
    # Tool declares nothing, but YAML adds a requirement for it.
    resolver = RequirementResolver([
        Requirement(match="run_bash", permissions=frozenset({"tool:bash"}), target="tool"),
    ])
    p = _enabled_plugin(resolver=resolver)
    denied = _call_tool(p, _Tool("run_bash"), _Ctx(perms=[]))
    permitted = _call_tool(p, _Tool("run_bash"), _Ctx(perms=["tool:bash"]))
    assert denied is not None and denied["status"] == "authz_denied", denied
    assert permitted is None, permitted
    print("OK test_tool_pep_yaml_requirement_gates_ungated_tool")


def test_tool_pep_ungated_tool_unaffected():
    p = _enabled_plugin()
    out = _call_tool(p, _Tool("read_file"), _Ctx(perms=[]))
    assert out is None, out  # baseline permits self-owned ungated call
    print("OK test_tool_pep_ungated_tool_unaffected")


# --- 4. Agent PEP ---------------------------------------------------------

class _Agent:
    def __init__(self, name):
        self.name = name


class _AgentCtx:
    def __init__(self, perms=()):
        self.state = {"temp:auth_principal": {
            "user_id": "alice", "tenant_id": "acme",
            "roles": [], "scopes": [], "permissions": list(perms),
        }}


def _call_agent(p, agent, ctx):
    return _run(p.before_agent_callback(agent=agent, callback_context=ctx))


def test_agent_pep_denies_handoff_without_permission():
    p = AuthzPlugin(
        pdp=AbacPolicyDecisionPoint([]),
        resolver=RequirementResolver([]),
        agent_requirements={"Explore": frozenset({"agent:explore"})},
    )
    p._enabled = True
    out = _call_agent(p, _Agent("Explore"), _AgentCtx(perms=[]))
    assert out is not None, "expected a denial Content"
    # It's a types.Content with a denial message.
    text = "".join(getattr(part, "text", "") or "" for part in getattr(out, "parts", []))
    assert "denied" in text.lower(), text
    print("OK test_agent_pep_denies_handoff_without_permission")


def test_agent_pep_allows_handoff_with_permission():
    p = AuthzPlugin(
        pdp=AbacPolicyDecisionPoint([]),
        resolver=RequirementResolver([]),
        agent_requirements={"Explore": frozenset({"agent:explore"})},
    )
    p._enabled = True
    out = _call_agent(p, _Agent("Explore"), _AgentCtx(perms=["agent:explore"]))
    assert out is None, out
    print("OK test_agent_pep_allows_handoff_with_permission")


def test_agent_pep_ungated_agent_allowed():
    # The coordinator (no requirement) is never blocked.
    p = AuthzPlugin(
        pdp=AbacPolicyDecisionPoint([]),
        resolver=RequirementResolver([]),
        agent_requirements={},
    )
    p._enabled = True
    out = _call_agent(p, _Agent("coordinator"), _AgentCtx(perms=[]))
    assert out is None, out
    print("OK test_agent_pep_ungated_agent_allowed")


def test_agent_pep_inert_when_disabled():
    p = AuthzPlugin(
        pdp=AbacPolicyDecisionPoint([]),
        resolver=RequirementResolver([]),
        agent_requirements={"Explore": frozenset({"agent:explore"})},
    )
    # _enabled stays False (ADK_CC_AUTHZ not set)
    out = _call_agent(p, _Agent("Explore"), _AgentCtx(perms=[]))
    assert out is None, out
    print("OK test_agent_pep_inert_when_disabled")


# --- 5. Auth extraction ---------------------------------------------------

def test_bearer_4th_segment_permissions():
    from adk_cc.service.auth import BearerTokenExtractor
    m = BearerTokenExtractor._parse_env("tok=alice:acme:admin:tool:deploy|agent:Explore")
    p = m["tok"]
    assert p.roles == frozenset({"admin"}), p.roles
    assert p.permissions == frozenset({"tool:deploy", "agent:Explore"}), p.permissions
    # 2-segment back-compat
    m2 = BearerTokenExtractor._parse_env("t2=bob:beta")
    assert m2["t2"].permissions == frozenset()
    print("OK test_bearer_4th_segment_permissions")


def _build_reflect_app(gateway_header):
    """A tiny app whose /perms route reflects the resolved principal's
    permissions, so a TestClient can assert what the auth middleware set."""
    from fastapi import FastAPI
    from starlette.requests import Request  # module-resolvable for get_type_hints
    from adk_cc.service.auth import (
        AuthPrincipal, BearerTokenExtractor, make_auth_middleware,
    )

    app = FastAPI()

    @app.get("/perms")
    async def perms(req: Request):
        principal = getattr(req.state, "adk_cc_auth", None)
        return {"permissions": sorted(getattr(principal, "permissions", []) or [])}

    tokmap = {
        "alicetok": AuthPrincipal(
            "alice", "acme", frozenset(), frozenset(), frozenset({"base:p"})
        )
    }
    extractor = BearerTokenExtractor(tokmap)
    app.add_middleware(
        make_auth_middleware(extractor, gateway_permissions_header=gateway_header)
    )
    return app


def test_gateway_header_merges_when_enabled():
    from starlette.testclient import TestClient

    c = TestClient(_build_reflect_app("X-Auth-Permissions"))
    r = c.get(
        "/perms",
        headers={
            "Authorization": "Bearer alicetok",
            "X-Auth-Permissions": "extra:1|extra:2",
        },
    )
    assert r.status_code == 200, r.text
    # base from the token UNION the gateway header.
    assert r.json()["permissions"] == ["base:p", "extra:1", "extra:2"], r.json()
    print("OK test_gateway_header_merges_when_enabled")


def test_gateway_header_ignored_when_not_enabled():
    from starlette.testclient import TestClient

    # gateway_header=None → the header must be ignored (no privilege grant).
    c = TestClient(_build_reflect_app(None))
    r = c.get(
        "/perms",
        headers={
            "Authorization": "Bearer alicetok",
            "X-Auth-Permissions": "extra:1|extra:2",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["permissions"] == ["base:p"], r.json()
    print("OK test_gateway_header_ignored_when_not_enabled")


# --- 6. Edge cases --------------------------------------------------------

def test_pdp_subject_extra_permissions_still_permits():
    # Holding MORE than required is fine (superset).
    pdp = AbacPolicyDecisionPoint([])
    d = pdp.authorize(
        _subject(["a", "b", "c"]), Action("x"), _owned_resource(),
        AuthzContext(required_permissions=frozenset({"a"})),
    )
    assert d.effect == "permit", d
    print("OK test_pdp_subject_extra_permissions_still_permits")


def test_pdp_empty_required_with_empty_subject_baseline():
    # No requirement + no permissions → baseline still governs (permit own).
    pdp = AbacPolicyDecisionPoint([])
    d = pdp.authorize(
        _subject([]), Action("x"), _owned_resource(),
        AuthzContext(required_permissions=frozenset()),
    )
    assert d.effect == "permit" and d.matched == "baseline:owner", d
    print("OK test_pdp_empty_required_with_empty_subject_baseline")


def test_pdp_requirement_blocks_cross_tenant_too():
    # Requirement is owner/tenant-independent: even a non-owned resource is
    # denied for lack of capability (and would be denied by closed-world
    # anyway — here we assert the requirement reason wins, evaluated first).
    pdp = AbacPolicyDecisionPoint([])
    foreign = Resource(type="tool", id="", owner_user_id="bob", tenant_id="beta")
    d = pdp.authorize(
        _subject([]), Action("x"), foreign,
        AuthzContext(required_permissions=frozenset({"need"})),
    )
    assert d.effect == "deny" and d.matched == "requirement", d
    print("OK test_pdp_requirement_blocks_cross_tenant_too")


def test_resolver_multiple_augments_accumulate():
    # Two augment entries matching the same name union together (+ base).
    reqs = [
        Requirement(match="mcp__*", permissions=frozenset({"p:1"}), target="tool"),
        Requirement(match="mcp__gh*", permissions=frozenset({"p:2"}), target="tool"),
    ]
    r = RequirementResolver(reqs)
    got = r.resolve("mcp__gh__issue", target="tool", base=frozenset({"p:0"}))
    assert got == frozenset({"p:0", "p:1", "p:2"}), got
    print("OK test_resolver_multiple_augments_accumulate")


def test_resolver_replace_after_augment_order_matters():
    # A `replace` later in file order discards earlier augments + base.
    reqs = [
        Requirement(match="x", permissions=frozenset({"early"}), target="tool"),
        Requirement(match="x", permissions=frozenset({"final"}), target="tool", mode="replace"),
    ]
    r = RequirementResolver(reqs)
    assert r.resolve("x", target="tool", base=frozenset({"b"})) == frozenset({"final"})
    print("OK test_resolver_replace_after_augment_order_matters")


def test_tool_pep_missing_principal_falls_to_local_subject():
    # No seeded principal at all → subject_from_state yields a bare "local"
    # subject with NO permissions, so a required tool is denied (fail-safe).
    p = _enabled_plugin()

    class _NoPrincipalCtx:
        state = {}  # nothing seeded

    out = _run(p.before_tool_callback(
        tool=_Tool("deploy", required=["tool:deploy"]),
        tool_args={}, tool_context=_NoPrincipalCtx(),
    ))
    assert out is not None and out["status"] == "authz_denied", out
    print("OK test_tool_pep_missing_principal_falls_to_local_subject")


def test_tool_pep_error_in_lookup_fails_closed():
    # A resolver that raises → the gate must DENY (fail-closed), not allow.
    class _BoomResolver(RequirementResolver):
        def resolve(self, *a, **k):
            raise RuntimeError("kaboom")

    p = _enabled_plugin(resolver=_BoomResolver([]))
    out = _call_tool(p, _Tool("deploy", required=["tool:deploy"]), _Ctx(perms=["tool:deploy"]))
    assert out is not None and out["status"] == "authz_denied", out
    assert "error" in out["reason"], out
    print("OK test_tool_pep_error_in_lookup_fails_closed")


def test_agent_pep_yaml_requirement_without_registry():
    # An agent gated purely by YAML (no registry entry) is still enforced.
    resolver = RequirementResolver([
        Requirement(match="verification", permissions=frozenset({"agent:verify"}), target="agent"),
    ])
    p = AuthzPlugin(pdp=AbacPolicyDecisionPoint([]), resolver=resolver, agent_requirements={})
    p._enabled = True
    denied = _call_agent(p, _Agent("verification"), _AgentCtx(perms=[]))
    allowed = _call_agent(p, _Agent("verification"), _AgentCtx(perms=["agent:verify"]))
    assert denied is not None and allowed is None
    print("OK test_agent_pep_yaml_requirement_without_registry")


def test_agent_pep_missing_state_fails_closed():
    # callback_context with no usable state but a required agent → deny.
    p = AuthzPlugin(
        pdp=AbacPolicyDecisionPoint([]), resolver=RequirementResolver([]),
        agent_requirements={"Explore": frozenset({"agent:explore"})},
    )
    p._enabled = True

    class _NoState:
        state = None

    out = _call_agent(p, _Agent("Explore"), _NoState())
    assert out is not None, "expected denial when required agent + no principal"
    print("OK test_agent_pep_missing_state_fails_closed")


def test_agent_pep_explicit_deny_policy_blocks_even_with_permission():
    # An explicit DENY policy on the agent action blocks even a holder.
    pdp = AbacPolicyDecisionPoint([
        AbacPolicy(effect="deny", action="invoke_agent:*", name="no-agents"),
    ])
    p = AuthzPlugin(
        pdp=pdp, resolver=RequirementResolver([]),
        agent_requirements={"Explore": frozenset({"agent:explore"})},
    )
    p._enabled = True
    out = _call_agent(p, _Agent("Explore"), _AgentCtx(perms=["agent:explore"]))
    assert out is not None, "explicit DENY policy must block the agent handoff"
    print("OK test_agent_pep_explicit_deny_policy_blocks_even_with_permission")


# --- 7. Replaceability: a swapped-in PDP governs BOTH tools and agents -----

class _RecordingPDP(PolicyDecisionPoint):
    """A drop-in custom PDP (stands in for OPA/Cerbos). Records every call
    and decides by a simple injected allow-list of permitted action names.
    Proves the PolicyDecisionPoint seam: the PEPs delegate ALL decisions
    here, for tools AND agents alike."""

    def __init__(self, allow_actions):
        self.allow_actions = set(allow_actions)
        self.calls = []

    def authorize(self, subject, action, resource, context):
        from adk_cc.authz import Decision
        self.calls.append((action.name, resource.type, tuple(sorted(context.required_permissions))))
        if action.name in self.allow_actions:
            return Decision("permit", "custom-allow", "custom")
        return Decision("deny", "custom-deny", "custom")


def test_custom_pdp_governs_tool_decisions():
    pdp = _RecordingPDP(allow_actions={"read_file"})
    p = _enabled_plugin(pdp=pdp)
    allowed = _call_tool(p, _Tool("read_file"), _Ctx(perms=[]))
    denied = _call_tool(p, _Tool("deploy", required=["tool:deploy"]), _Ctx(perms=["tool:deploy"]))
    assert allowed is None, allowed              # custom PDP permitted
    assert denied is not None and denied["status"] == "authz_denied", denied
    # The custom PDP actually saw both calls, with the resolved requirement.
    names = [c[0] for c in pdp.calls]
    assert "read_file" in names and "deploy" in names, pdp.calls
    print("OK test_custom_pdp_governs_tool_decisions")


def test_custom_pdp_governs_agent_decisions():
    # The whole point of routing agents through the PDP: a swapped PDP must
    # govern agent handoffs too, not just tools.
    pdp = _RecordingPDP(allow_actions={"invoke_agent:Explore"})
    p = AuthzPlugin(
        pdp=pdp, resolver=RequirementResolver([]),
        agent_requirements={"Explore": frozenset({"agent:explore"}),
                            "verification": frozenset({"agent:verify"})},
    )
    p._enabled = True
    allowed = _call_agent(p, _Agent("Explore"), _AgentCtx(perms=["agent:explore"]))
    denied = _call_agent(p, _Agent("verification"), _AgentCtx(perms=["agent:verify"]))
    assert allowed is None, "custom PDP permitted Explore"
    assert denied is not None, "custom PDP denied verification"
    # Custom PDP saw the agent actions with type 'agent'.
    agent_calls = [c for c in pdp.calls if c[1] == "agent"]
    assert {c[0] for c in agent_calls} == {"invoke_agent:Explore", "invoke_agent:verification"}, pdp.calls
    print("OK test_custom_pdp_governs_agent_decisions")


if __name__ == "__main__":
    test_pdp_requirement_met_permits()
    test_pdp_requirement_missing_denies_despite_ownership()
    test_pdp_requirement_and_semantics()
    test_pdp_explicit_permit_overrides_requirement()
    test_pdp_explicit_deny_still_wins_over_requirement()
    test_pdp_no_requirement_baseline_unchanged()
    test_resolver_augment_replace_target()
    test_tool_pep_denies_when_lacking_meta_permission()
    test_tool_pep_permits_when_holding_meta_permission()
    test_tool_pep_yaml_requirement_gates_ungated_tool()
    test_tool_pep_ungated_tool_unaffected()
    test_agent_pep_denies_handoff_without_permission()
    test_agent_pep_allows_handoff_with_permission()
    test_agent_pep_ungated_agent_allowed()
    test_agent_pep_inert_when_disabled()
    test_bearer_4th_segment_permissions()
    test_gateway_header_merges_when_enabled()
    test_gateway_header_ignored_when_not_enabled()
    # edge cases
    test_pdp_subject_extra_permissions_still_permits()
    test_pdp_empty_required_with_empty_subject_baseline()
    test_pdp_requirement_blocks_cross_tenant_too()
    test_resolver_multiple_augments_accumulate()
    test_resolver_replace_after_augment_order_matters()
    test_tool_pep_missing_principal_falls_to_local_subject()
    test_tool_pep_error_in_lookup_fails_closed()
    test_agent_pep_yaml_requirement_without_registry()
    test_agent_pep_missing_state_fails_closed()
    test_agent_pep_explicit_deny_policy_blocks_even_with_permission()
    # replaceability
    test_custom_pdp_governs_tool_decisions()
    test_custom_pdp_governs_agent_decisions()
    print("\nall authz-capabilities tests passed")
