"""Tests for the ABAC Policy Decision Point.

Hand-rolled (no pytest), runnable with the venv python.
"""

from __future__ import annotations

import os

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.authz import (
    AbacPolicy,
    AbacPolicyDecisionPoint,
    Action,
    AuthzContext,
    Resource,
    Subject,
)

_CTX = AuthzContext()


def _sub(user="alice", tenant="acme", roles=(), scopes=()):
    return Subject(user_id=user, tenant_id=tenant, roles=frozenset(roles), scopes=frozenset(scopes))


def _res(rtype="artifact", rid="x", owner=None, tenant=None, **attrs):
    return Resource(type=rtype, id=rid, owner_user_id=owner, tenant_id=tenant, attrs=attrs)


def test_owner_baseline_permit():
    pdp = AbacPolicyDecisionPoint([])
    d = pdp.authorize(_sub(), Action("read_artifact"), _res(owner="alice"), _CTX)
    assert d.permitted and d.matched == "baseline:owner", d
    print("OK test_owner_baseline_permit")


def test_same_tenant_baseline_permit():
    pdp = AbacPolicyDecisionPoint([])
    d = pdp.authorize(_sub(tenant="acme"), Action("read_session"), _res(tenant="acme"), _CTX)
    assert d.permitted and d.matched == "baseline:tenant"
    print("OK test_same_tenant_baseline_permit")


def test_cross_tenant_default_deny():
    pdp = AbacPolicyDecisionPoint([])
    # bob@beta acting on a resource owned by alice@acme — no policy → deny
    d = pdp.authorize(_sub(user="bob", tenant="beta"), Action("read_artifact"),
                      _res(owner="alice", tenant="acme"), _CTX)
    assert not d.permitted and "default deny" in d.reason
    print("OK test_cross_tenant_default_deny")


def test_role_grants_cross_tenant():
    pdp = AbacPolicyDecisionPoint([
        AbacPolicy(effect="permit", roles=frozenset({"admin"}), action="read_*", name="admin-read"),
    ])
    d = pdp.authorize(_sub(user="bob", tenant="beta", roles={"admin"}),
                      Action("read_artifact"), _res(owner="alice", tenant="acme"), _CTX)
    assert d.permitted and d.matched == "admin-read"
    # but a non-admin still denied
    d2 = pdp.authorize(_sub(user="bob", tenant="beta"),
                       Action("read_artifact"), _res(owner="alice", tenant="acme"), _CTX)
    assert not d2.permitted
    print("OK test_role_grants_cross_tenant")


def test_deny_beats_permit():
    pdp = AbacPolicyDecisionPoint([
        AbacPolicy(effect="permit", action="*", name="allow-all"),
        AbacPolicy(effect="deny", action="run_bash", resource="rm *", name="no-rm"),
    ])
    # even with allow-all, the deny wins (deny scanned first regardless of order)
    d = pdp.authorize(_sub(), Action("run_bash"), _res(rtype="command", rid="rm -rf /"), _CTX)
    assert not d.permitted and d.matched == "no-rm"
    # a non-matching command falls to the permit
    d2 = pdp.authorize(_sub(), Action("run_bash"), _res(rtype="command", rid="ls"), _CTX)
    assert d2.permitted and d2.matched == "allow-all"
    print("OK test_deny_beats_permit")


def test_scope_match():
    pdp = AbacPolicyDecisionPoint([
        AbacPolicy(effect="permit", scopes=frozenset({"write:artifacts"}),
                   action="save_as_artifact", name="scoped-write"),
    ])
    ok = pdp.authorize(_sub(scopes={"write:artifacts"}), Action("save_as_artifact"), _res(), _CTX)
    no = pdp.authorize(_sub(scopes={"read:artifacts"}), Action("save_as_artifact"), _res(), _CTX)
    assert ok.permitted and not no.permitted
    print("OK test_scope_match")


def test_action_and_resource_glob():
    pdp = AbacPolicyDecisionPoint([
        AbacPolicy(effect="deny", action="write_file", resource="/etc/*", name="protect-etc"),
    ])
    d = pdp.authorize(_sub(), Action("write_file"), _res(rtype="file", rid="/etc/passwd"), _CTX)
    assert not d.permitted and d.matched == "protect-etc"
    # /home write isn't matched by the deny → owner/tenant baseline... none set → default deny
    d2 = pdp.authorize(_sub(), Action("write_file"), _res(rtype="file", rid="/home/alice/x"), _CTX)
    assert not d2.permitted and d2.matched is None  # closed-world
    print("OK test_action_and_resource_glob")


def test_owner_false_predicate():
    # policy that only applies when subject does NOT own the resource
    pdp = AbacPolicyDecisionPoint([
        AbacPolicy(effect="deny", owner=False, resource_type="artifact", name="not-owner-deny"),
    ])
    mine = pdp.authorize(_sub(), Action("read_artifact"), _res(owner="alice"), _CTX)
    theirs = pdp.authorize(_sub(), Action("read_artifact"), _res(owner="carol", tenant="zzz"), _CTX)
    assert mine.permitted  # owner baseline
    assert not theirs.permitted and theirs.matched == "not-owner-deny"
    print("OK test_owner_false_predicate")


if __name__ == "__main__":
    test_owner_baseline_permit()
    test_same_tenant_baseline_permit()
    test_cross_tenant_default_deny()
    test_role_grants_cross_tenant()
    test_deny_beats_permit()
    test_scope_match()
    test_action_and_resource_glob()
    test_owner_false_predicate()
    print("\nall authz-pdp tests passed")
