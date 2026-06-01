"""Tests for the authZ PIP (attributes) + PAP (policy loader).

Hand-rolled (no pytest), runnable with the venv python.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.authz import (
    load_policies_from_yaml,
    resource_from_tool,
    subject_from_state,
)


# --- subject_from_state (PIP) --------------------------------------------

def test_subject_from_principal():
    state = {"temp:auth_principal": {
        "user_id": "alice", "tenant_id": "acme",
        "roles": ["admin", "deployer"], "scopes": ["read:x"],
    }}
    s = subject_from_state(state)
    assert s.user_id == "alice" and s.tenant_id == "acme"
    assert s.roles == frozenset({"admin", "deployer"}) and s.scopes == frozenset({"read:x"})
    print("OK test_subject_from_principal")


def test_subject_falls_back_to_tenant_context():
    import types as pytypes
    tenant = pytypes.SimpleNamespace(user_id="bob", tenant_id="beta")
    s = subject_from_state({"temp:tenant_context": tenant})
    assert s.user_id == "bob" and s.tenant_id == "beta" and not s.roles
    print("OK test_subject_falls_back_to_tenant_context")


def test_subject_bare_default():
    s = subject_from_state({})
    assert s.user_id == "local" and s.tenant_id == "local"
    print("OK test_subject_bare_default")


# --- resource_from_tool (PIP) --------------------------------------------

def test_resource_uses_rule_key_extractor():
    s = subject_from_state({"temp:auth_principal": {"user_id": "alice", "tenant_id": "acme"}})
    r = resource_from_tool("write_file", {"path": "/etc/passwd"}, s)
    assert r.type == "file" and r.id == "/etc/passwd"
    assert r.owner_user_id == "alice" and r.tenant_id == "acme"
    print("OK test_resource_uses_rule_key_extractor")


def test_resource_run_bash_command():
    s = subject_from_state({})
    r = resource_from_tool("run_bash", {"command": "rm -rf /"}, s)
    assert r.type == "command" and r.id == "rm -rf /"
    print("OK test_resource_run_bash_command")


def test_resource_unknown_tool_empty_id():
    s = subject_from_state({})
    r = resource_from_tool("mcp__github__create_issue", {"title": "x"}, s)
    assert r.type == "tool" and r.id == ""  # no extractor → action+subject only
    print("OK test_resource_unknown_tool_empty_id")


# --- policy_loader (PAP) -------------------------------------------------

def _write(yaml_text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(yaml_text)
    f.close()
    return f.name


def test_load_policies():
    path = _write(
        "policies:\n"
        "  - effect: deny\n"
        "    action: run_bash\n"
        "    resource: 'rm *'\n"
        "  - effect: permit\n"
        "    roles: [admin]\n"
        "    action: 'read_*'\n"
        "  - effect: permit\n"
        "    scopes: 'write:artifacts read:artifacts'\n"
        "    action: save_as_artifact\n"
    )
    pols = load_policies_from_yaml(path)
    assert len(pols) == 3
    assert pols[0].effect == "deny" and pols[0].action == "run_bash" and pols[0].resource == "rm *"
    assert pols[1].effect == "permit" and pols[1].roles == frozenset({"admin"})
    assert pols[2].scopes == frozenset({"write:artifacts", "read:artifacts"})
    print("OK test_load_policies")


def test_load_policies_empty_when_absent():
    path = _write("rules:\n  - tool: run_bash\n    behavior: deny\n")  # perm-only file
    assert load_policies_from_yaml(path) == []
    print("OK test_load_policies_empty_when_absent")


def test_load_policies_bad_effect_raises():
    path = _write("policies:\n  - effect: maybe\n    action: x\n")
    try:
        load_policies_from_yaml(path)
        assert False, "should have raised"
    except ValueError as e:
        assert "effect" in str(e)
    print("OK test_load_policies_bad_effect_raises")


if __name__ == "__main__":
    test_subject_from_principal()
    test_subject_falls_back_to_tenant_context()
    test_subject_bare_default()
    test_resource_uses_rule_key_extractor()
    test_resource_run_bash_command()
    test_resource_unknown_tool_empty_id()
    test_load_policies()
    test_load_policies_empty_when_absent()
    test_load_policies_bad_effect_raises()
    print("\nall authz-attributes tests passed")
