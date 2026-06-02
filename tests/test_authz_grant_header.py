"""Tests for the gateway grant-header edge adapter (service/grant_header_auth).

Covers the three pieces that wire the gateway's grant format into the
generic authZ layer:
  1. flatten_grant — the held grant header → capability strings (authYn,
     role/source/dept, multi-batch union, real names, dept tolerance).
  2. ScopedLevelRequirementProvider — per-agent, exact-level requirement
     strings that line up with the flattened grant; per-agent tool isolation;
     closed_world.
  3. GrantHeaderExtractor — end-to-end via a FastAPI TestClient: the auth
     middleware builds the principal and a route reflects its permissions.
  4. Full-loop through the real AuthzPlugin: read_file permitted under the
     granting agent, DENIED under a different agent (per-agent scoping).

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
    ScopedLevelRequirementProvider,
    flatten_grant,
)


def _run(coro):
    return asyncio.run(coro)


def _sample(service="Explore", func="read_file", granted=True, dept=None):
    item = {
        "authYn": granted,
        "authType": "MANAGER",
        "objectId": "randomid",
        "authLevel": [1],
        "authSource": "PERSONAL",
        "authSourceDeptCode": dept,
        "authSourceDeptCodes": None,
        "serviceName": service,
        "detailedAuth": [
            {"funcId": func, "authName": "u", "funcName": "f", "authLevel": 1}
        ],
    }
    return [{"resolvedAt": "2024-06-17T12:00:00Z", "authList": [item]}]


# --- 1. flatten_grant -----------------------------------------------------

def test_flatten_basic_sample():
    held = flatten_grant(_sample())
    assert held == frozenset({
        "svc:Explore:level:1",
        "svc:Explore:role:MANAGER",
        "svc:Explore:source:PERSONAL",
        "svc:Explore:func:read_file:level:1",
    }), held
    print("OK test_flatten_basic_sample")


def test_flatten_skips_authyn_false():
    held = flatten_grant(_sample(granted=False))
    assert held == frozenset(), held
    print("OK test_flatten_skips_authyn_false")


def test_flatten_dept_present_service_scoped():
    held = flatten_grant(_sample(dept="DEPT42"))
    assert "svc:Explore:dept:DEPT42" in held, held
    print("OK test_flatten_dept_present_service_scoped")


def test_flatten_dept_plural_and_singular():
    item = {
        "authYn": True, "serviceName": "A", "authLevel": [],
        "authSourceDeptCode": "D1",
        "authSourceDeptCodes": ["D2", "D3", None],
        "detailedAuth": [],
    }
    held = flatten_grant([{"authList": [item]}])
    assert held == frozenset({"svc:A:dept:D1", "svc:A:dept:D2", "svc:A:dept:D3"}), held
    print("OK test_flatten_dept_plural_and_singular")


def test_flatten_multi_batch_union():
    auth = _sample("A", "t1") + _sample("B", "t2")
    held = flatten_grant(auth)
    assert "svc:A:func:t1:level:1" in held
    assert "svc:B:func:t2:level:1" in held
    print("OK test_flatten_multi_batch_union")


def test_flatten_missing_service_skipped():
    held = flatten_grant([{"authList": [{"authYn": True, "detailedAuth": []}]}])
    assert held == frozenset(), held
    print("OK test_flatten_missing_service_skipped")


def test_flatten_empty_and_none():
    assert flatten_grant([]) == frozenset()
    assert flatten_grant(None) == frozenset()
    print("OK test_flatten_empty_and_none")


# --- 2. ScopedLevelRequirementProvider ------------------------------------

def test_provider_tool_and_agent_line_up_with_grant():
    held = flatten_grant(_sample())
    prov = ScopedLevelRequirementProvider(
        tool_levels={("Explore", "read_file"): 1},
        agent_levels={"Explore": 1},
        agent_roles={"Explore": "MANAGER"},
    )
    rt = prov.for_tool("read_file", invoking_agent="Explore")
    ra = prov.for_agent("Explore")
    assert rt == frozenset({"svc:Explore:func:read_file:level:1"})
    assert rt <= held and ra <= held
    print("OK test_provider_tool_and_agent_line_up_with_grant")


def test_provider_per_agent_tool_isolation():
    # The SAME tool under a different agent yields a different requirement.
    prov = ScopedLevelRequirementProvider(
        tool_levels={("Explore", "read_file"): 1},
    )
    under_explore = prov.for_tool("read_file", invoking_agent="Explore")
    under_other = prov.for_tool("read_file", invoking_agent="verification")
    assert under_explore == frozenset({"svc:Explore:func:read_file:level:1"})
    assert under_other == frozenset()  # not mapped for that agent → ungated here
    print("OK test_provider_per_agent_tool_isolation")


def test_provider_closed_world_denies_unmapped():
    prov = ScopedLevelRequirementProvider(closed_world=True)
    rt = prov.for_tool("anything", invoking_agent="X")
    ra = prov.for_agent("Y")
    # Unsatisfiable sentinel → no grant can match → PDP denies.
    assert rt and ra and all(":DENY" in p for p in rt | ra), (rt, ra)
    print("OK test_provider_closed_world_denies_unmapped")


# --- 3. GrantHeaderExtractor (end-to-end via middleware) ------------------

def _reflect_app():
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

    app.add_middleware(make_auth_middleware(GrantHeaderExtractor()))
    return app


def test_extractor_end_to_end():
    from starlette.testclient import TestClient

    c = TestClient(_reflect_app())
    grant = json.dumps({"auth": _sample()})
    r = c.get("/perms", headers={
        "X-Auth-Grant": grant,
        "X-Auth-User": "alice",
        "X-Auth-Tenant": "acme",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"] == "alice"
    assert "svc:Explore:func:read_file:level:1" in body["permissions"], body
    print("OK test_extractor_end_to_end")


def test_extractor_missing_header_401():
    from starlette.testclient import TestClient

    c = TestClient(_reflect_app())
    r = c.get("/perms", headers={"X-Auth-User": "alice"})  # no grant
    assert r.status_code == 401, r.text
    print("OK test_extractor_missing_header_401")


def test_extractor_malformed_header_401():
    from starlette.testclient import TestClient

    c = TestClient(_reflect_app())
    r = c.get("/perms", headers={"X-Auth-Grant": "not-json", "X-Auth-User": "a"})
    assert r.status_code == 401, r.text
    print("OK test_extractor_malformed_header_401")


# --- 4. Full loop through the real AuthzPlugin (per-agent scoping) --------

class _Tool:
    def __init__(self, name):
        self.name = name
        self.meta = type("M", (), {"name": name, "required_permissions": frozenset()})()


class _ToolCtx:
    """Simulates a tool call from a specific invoking agent, with the grant
    flattened into seeded principal state (as TenancyPlugin would)."""

    def __init__(self, held, agent_name):
        self.state = {"temp:auth_principal": {
            "user_id": "alice", "tenant_id": "acme",
            "roles": [], "scopes": [], "permissions": sorted(held),
        }}
        self.agent_name = agent_name


def _plugin_with_grant_provider():
    prov = ScopedLevelRequirementProvider(
        tool_levels={("Explore", "read_file"): 1},
    )
    p = AuthzPlugin(pdp=AbacPolicyDecisionPoint([]), requirement_provider=prov)
    p._enabled = True
    return p


def test_full_loop_tool_permitted_under_granting_agent():
    held = flatten_grant(_sample("Explore", "read_file"))
    p = _plugin_with_grant_provider()
    out = _run(p.before_tool_callback(
        tool=_Tool("read_file"), tool_args={},
        tool_context=_ToolCtx(held, agent_name="Explore"),
    ))
    assert out is None, out  # permitted
    print("OK test_full_loop_tool_permitted_under_granting_agent")


def test_full_loop_tool_denied_under_different_agent():
    # Grant is for read_file UNDER Explore. The same tool invoked under a
    # different agent requires svc:other:func:read_file:level:1, which the
    # user does NOT hold → denied. This is the per-agent guarantee.
    held = flatten_grant(_sample("Explore", "read_file"))
    prov = ScopedLevelRequirementProvider(
        tool_levels={("Explore", "read_file"): 1, ("other", "read_file"): 1},
    )
    p = AuthzPlugin(pdp=AbacPolicyDecisionPoint([]), requirement_provider=prov)
    p._enabled = True
    out = _run(p.before_tool_callback(
        tool=_Tool("read_file"), tool_args={},
        tool_context=_ToolCtx(held, agent_name="other"),
    ))
    assert out is not None and out["status"] == "authz_denied", out
    print("OK test_full_loop_tool_denied_under_different_agent")


if __name__ == "__main__":
    test_flatten_basic_sample()
    test_flatten_skips_authyn_false()
    test_flatten_dept_present_service_scoped()
    test_flatten_dept_plural_and_singular()
    test_flatten_multi_batch_union()
    test_flatten_missing_service_skipped()
    test_flatten_empty_and_none()
    test_provider_tool_and_agent_line_up_with_grant()
    test_provider_per_agent_tool_isolation()
    test_provider_closed_world_denies_unmapped()
    test_extractor_end_to_end()
    test_extractor_missing_header_401()
    test_extractor_malformed_header_401()
    test_full_loop_tool_permitted_under_granting_agent()
    test_full_loop_tool_denied_under_different_agent()
    print("\nall authz-grant-header tests passed")
