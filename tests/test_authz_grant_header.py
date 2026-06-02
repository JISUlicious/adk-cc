"""Tests for the gateway grant-header edge adapter (service/grant_header_auth).

Gateway is FULLY AUTHORITATIVE → presence-based: a (agent, tool) pair in the
grant is allowed, otherwise denied. No level/role/dept comparison.

Covers:
  1. flatten_grant — held grant header → presence strings (authYn skip,
     multi-batch union, real names, level/role/dept ignored).
  2. PresenceRequirementProvider — requirement derived from the call;
     per-agent tool isolation; entry-agent exemption; missing-agent
     fail-closed.
  3. GrantHeaderExtractor — end-to-end via TestClient.
  4. env wiring — ADK_CC_GRANT_HEADER selects the presence provider.
  5. full loop through the real AuthzPlugin — permitted under granting
     agent, DENIED under a different agent; exempt entry agent passes.

Hand-rolled (no pytest), run with the venv python.
"""

from __future__ import annotations

import asyncio
import json
import os

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from starlette.requests import Request  # noqa: E402,F401 — get_type_hints resolution

from adk_cc.authz import AbacPolicyDecisionPoint
from adk_cc.plugins.authz import AuthzPlugin
from adk_cc.service.grant_header_auth import (
    GrantHeaderExtractor,
    PresenceRequirementProvider,
    flatten_grant,
    grant_extractor_from_env,
    grant_provider_from_env,
)


def _run(coro):
    return asyncio.run(coro)


def _sample(service="Explore", func="read_file", granted=True):
    item = {
        "authYn": granted,
        "authType": "MANAGER",          # ignored by presence scheme
        "objectId": "randomid",         # ignored
        "authLevel": [1],               # ignored
        "authSource": "PERSONAL",       # ignored
        "authSourceDeptCode": "D1",     # ignored
        "authSourceDeptCodes": None,
        "serviceName": service,
        "detailedAuth": [
            {"funcId": func, "authName": "u", "funcName": "f", "authLevel": 1}
        ],
    }
    return [{"resolvedAt": "2024-06-17T12:00:00Z", "authList": [item]}]


# --- 1. flatten_grant -----------------------------------------------------

def test_flatten_presence_only():
    held = flatten_grant(_sample())
    # Only presence strings — no level/role/source/dept noise.
    assert held == frozenset({"svc:Explore", "svc:Explore:func:read_file"}), held
    print("OK test_flatten_presence_only")


def test_flatten_skips_authyn_false():
    assert flatten_grant(_sample(granted=False)) == frozenset()
    print("OK test_flatten_skips_authyn_false")


def test_flatten_multi_batch_union():
    auth = _sample("A", "t1") + _sample("B", "t2")
    held = flatten_grant(auth)
    assert held == frozenset({"svc:A", "svc:A:func:t1", "svc:B", "svc:B:func:t2"}), held
    print("OK test_flatten_multi_batch_union")


def test_flatten_missing_service_skipped():
    assert flatten_grant([{"authList": [{"authYn": True, "detailedAuth": []}]}]) == frozenset()
    print("OK test_flatten_missing_service_skipped")


def test_flatten_empty_and_none():
    assert flatten_grant([]) == frozenset()
    assert flatten_grant(None) == frozenset()
    print("OK test_flatten_empty_and_none")


def test_flatten_service_without_funcs():
    auth = [{"authList": [{"authYn": True, "serviceName": "A", "detailedAuth": []}]}]
    assert flatten_grant(auth) == frozenset({"svc:A"}), flatten_grant(auth)
    print("OK test_flatten_service_without_funcs")


# --- 2. PresenceRequirementProvider ---------------------------------------

def test_provider_requirement_lines_up_with_grant():
    held = flatten_grant(_sample())
    prov = PresenceRequirementProvider()
    rt = prov.for_tool("read_file", invoking_agent="Explore")
    ra = prov.for_agent("Explore")
    assert rt == frozenset({"svc:Explore:func:read_file"})
    assert ra == frozenset({"svc:Explore"})
    assert rt <= held and ra <= held
    print("OK test_provider_requirement_lines_up_with_grant")


def test_provider_per_agent_tool_isolation():
    prov = PresenceRequirementProvider()
    assert prov.for_tool("read_file", invoking_agent="Explore") == frozenset({"svc:Explore:func:read_file"})
    assert prov.for_tool("read_file", invoking_agent="other") == frozenset({"svc:other:func:read_file"})
    print("OK test_provider_per_agent_tool_isolation")


def test_provider_exempts_entry_agent():
    prov = PresenceRequirementProvider(exempt_agents={"coordinator"})
    assert prov.for_agent("coordinator") == frozenset()
    assert prov.for_agent("Explore") == frozenset({"svc:Explore"})
    print("OK test_provider_exempts_entry_agent")


def test_provider_missing_agent_fails_closed():
    # A tool call with no invoking agent → unsatisfiable requirement (deny).
    prov = PresenceRequirementProvider()
    req = prov.for_tool("read_file", invoking_agent=None)
    assert req and all(p.startswith("svc:?") for p in req), req
    print("OK test_provider_missing_agent_fails_closed")


# --- 3. GrantHeaderExtractor (end-to-end via middleware) ------------------

def _reflect_app(extractor):
    from fastapi import FastAPI
    from adk_cc.service.auth import make_auth_middleware

    app = FastAPI()

    @app.get("/perms")
    async def perms(req: Request):
        p = getattr(req.state, "adk_cc_auth", None)
        return {
            "user": getattr(p, "user_id", None),
            "permissions": sorted(getattr(p, "permissions", []) or []),
        }

    app.add_middleware(make_auth_middleware(extractor))
    return app


def test_extractor_end_to_end():
    from starlette.testclient import TestClient

    c = TestClient(_reflect_app(GrantHeaderExtractor()))
    grant = json.dumps({"auth": _sample()})
    r = c.get("/perms", headers={
        "X-Auth-Grant": grant, "X-Auth-User": "alice", "X-Auth-Tenant": "acme",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"] == "alice"
    assert set(body["permissions"]) == {"svc:Explore", "svc:Explore:func:read_file"}, body
    print("OK test_extractor_end_to_end")


def test_extractor_missing_header_401():
    from starlette.testclient import TestClient

    c = TestClient(_reflect_app(GrantHeaderExtractor()))
    r = c.get("/perms", headers={"X-Auth-User": "alice"})
    assert r.status_code == 401, r.text
    print("OK test_extractor_missing_header_401")


def test_extractor_malformed_header_401():
    from starlette.testclient import TestClient

    c = TestClient(_reflect_app(GrantHeaderExtractor()))
    r = c.get("/perms", headers={"X-Auth-Grant": "not-json", "X-Auth-User": "a"})
    assert r.status_code == 401, r.text
    print("OK test_extractor_malformed_header_401")


# --- 4. env wiring --------------------------------------------------------

def test_env_factories_off_by_default():
    os.environ.pop("ADK_CC_GRANT_HEADER", None)
    assert grant_provider_from_env() is None
    assert grant_extractor_from_env() is None
    print("OK test_env_factories_off_by_default")


def test_env_factories_on_with_flag():
    os.environ["ADK_CC_GRANT_HEADER"] = "1"
    os.environ["ADK_CC_GRANT_EXEMPT_AGENTS"] = "root,coordinator"
    try:
        prov = grant_provider_from_env()
        ext = grant_extractor_from_env()
        assert isinstance(prov, PresenceRequirementProvider)
        assert isinstance(ext, GrantHeaderExtractor)
        # exemptions parsed from env
        assert prov.for_agent("root") == frozenset()
        assert prov.for_agent("coordinator") == frozenset()
        assert prov.for_agent("Explore") == frozenset({"svc:Explore"})
    finally:
        os.environ.pop("ADK_CC_GRANT_HEADER", None)
        os.environ.pop("ADK_CC_GRANT_EXEMPT_AGENTS", None)
    print("OK test_env_factories_on_with_flag")


# --- 5. full loop through the real AuthzPlugin ----------------------------

class _Tool:
    def __init__(self, name):
        self.name = name
        self.meta = type("M", (), {"name": name, "required_permissions": frozenset()})()


class _ToolCtx:
    def __init__(self, held, agent_name):
        self.state = {"temp:auth_principal": {
            "user_id": "alice", "tenant_id": "acme",
            "roles": [], "scopes": [], "permissions": sorted(held),
        }}
        self.agent_name = agent_name


class _Agent:
    def __init__(self, name):
        self.name = name


class _AgentCtx:
    def __init__(self, held):
        self.state = {"temp:auth_principal": {
            "user_id": "alice", "tenant_id": "acme",
            "roles": [], "scopes": [], "permissions": sorted(held),
        }}


def _grant_plugin():
    p = AuthzPlugin(
        pdp=AbacPolicyDecisionPoint([]),
        requirement_provider=PresenceRequirementProvider(exempt_agents={"coordinator"}),
    )
    p._enabled = True
    return p


def test_full_loop_tool_permitted_under_granting_agent():
    held = flatten_grant(_sample("Explore", "read_file"))
    out = _run(_grant_plugin().before_tool_callback(
        tool=_Tool("read_file"), tool_args={}, tool_context=_ToolCtx(held, "Explore"),
    ))
    assert out is None, out
    print("OK test_full_loop_tool_permitted_under_granting_agent")


def test_full_loop_tool_denied_under_different_agent():
    # Grant is for read_file UNDER Explore; invoked under 'verification' it
    # requires svc:verification:func:read_file, which the user lacks → deny.
    held = flatten_grant(_sample("Explore", "read_file"))
    out = _run(_grant_plugin().before_tool_callback(
        tool=_Tool("read_file"), tool_args={}, tool_context=_ToolCtx(held, "verification"),
    ))
    assert out is not None and out["status"] == "authz_denied", out
    print("OK test_full_loop_tool_denied_under_different_agent")


def test_full_loop_agent_handoff_permitted_and_denied():
    held = flatten_grant(_sample("Explore", "read_file"))  # grants svc:Explore
    p = _grant_plugin()
    ok = _run(p.before_agent_callback(agent=_Agent("Explore"), callback_context=_AgentCtx(held)))
    no = _run(p.before_agent_callback(agent=_Agent("verification"), callback_context=_AgentCtx(held)))
    assert ok is None, ok               # Explore granted
    assert no is not None, no           # verification not in grant → denied
    print("OK test_full_loop_agent_handoff_permitted_and_denied")


def test_full_loop_entry_agent_exempt():
    # The coordinator (entry agent) is exempt → never blocked, even with an
    # empty grant.
    out = _run(_grant_plugin().before_agent_callback(
        agent=_Agent("coordinator"), callback_context=_AgentCtx(frozenset()),
    ))
    assert out is None, out
    print("OK test_full_loop_entry_agent_exempt")


if __name__ == "__main__":
    test_flatten_presence_only()
    test_flatten_skips_authyn_false()
    test_flatten_multi_batch_union()
    test_flatten_missing_service_skipped()
    test_flatten_empty_and_none()
    test_flatten_service_without_funcs()
    test_provider_requirement_lines_up_with_grant()
    test_provider_per_agent_tool_isolation()
    test_provider_exempts_entry_agent()
    test_provider_missing_agent_fails_closed()
    test_extractor_end_to_end()
    test_extractor_missing_header_401()
    test_extractor_malformed_header_401()
    test_env_factories_off_by_default()
    test_env_factories_on_with_flag()
    test_full_loop_tool_permitted_under_granting_agent()
    test_full_loop_tool_denied_under_different_agent()
    test_full_loop_agent_handoff_permitted_and_denied()
    test_full_loop_entry_agent_exempt()
    print("\nall authz-grant-header tests passed")
