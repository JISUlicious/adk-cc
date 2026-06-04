"""Tests for the backend-agnostic sandbox env/credential spec
(sandbox/sandbox_env.py).

Covers resolution (static / passthrough / per-tenant credentials), source
precedence, graceful skipping of missing sources, the env-var factory, and
the KV/JSON parser. Hand-rolled (no pytest).
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.sandbox.sandbox_env import (
    SandboxEnvSpec,
    sandbox_env_spec_from_env,
    _parse_kv,
)
from adk_cc.credentials.impls import InMemoryCredentialProvider


# --- resolution ----------------------------------------------------------

def test_empty_spec_is_empty_and_resolves_blank():
    spec = SandboxEnvSpec()
    assert spec.is_empty()
    assert asyncio.run(spec.resolve(tenant_id="t")) == {}
    print("OK empty_spec_is_empty_and_resolves_blank")


def test_static_literals():
    spec = SandboxEnvSpec(static={"TZ": "UTC", "LANG": "C.UTF-8"})
    assert not spec.is_empty()
    out = asyncio.run(spec.resolve(tenant_id="t"))
    assert out == {"TZ": "UTC", "LANG": "C.UTF-8"}, out
    print("OK static_literals")


def test_passthrough_present_and_absent():
    host = {"PRESENT": "yes"}  # ABSENT intentionally missing
    spec = SandboxEnvSpec(passthrough=("PRESENT", "ABSENT"))
    out = asyncio.run(spec.resolve(tenant_id="t", host_env=host))
    # present copied; absent skipped (not an error)
    assert out == {"PRESENT": "yes"}, out
    print("OK passthrough_present_and_absent")


def test_credentials_resolved_per_tenant():
    prov = InMemoryCredentialProvider(shared=False)
    asyncio.run(prov.put(tenant_id="acme", key="gh_pat", value="secret-acme"))
    asyncio.run(prov.put(tenant_id="beta", key="gh_pat", value="secret-beta"))
    spec = SandboxEnvSpec(credentials={"GITHUB_TOKEN": "gh_pat"})
    a = asyncio.run(spec.resolve(tenant_id="acme", credentials=prov))
    b = asyncio.run(spec.resolve(tenant_id="beta", credentials=prov))
    assert a == {"GITHUB_TOKEN": "secret-acme"}, a
    assert b == {"GITHUB_TOKEN": "secret-beta"}, b  # per-tenant isolation
    print("OK credentials_resolved_per_tenant")


def test_credential_missing_key_skipped():
    prov = InMemoryCredentialProvider(shared=False)  # nothing registered
    spec = SandboxEnvSpec(credentials={"GITHUB_TOKEN": "gh_pat"})
    out = asyncio.run(spec.resolve(tenant_id="acme", credentials=prov))
    assert out == {}, out  # missing secret skipped, not fatal
    print("OK credential_missing_key_skipped")


def test_credential_without_provider_skipped():
    # static-token / dev mode: no provider → credential entries skipped,
    # but static/passthrough still apply.
    spec = SandboxEnvSpec(
        static={"TZ": "UTC"}, credentials={"GITHUB_TOKEN": "gh_pat"}
    )
    out = asyncio.run(spec.resolve(tenant_id="acme", credentials=None))
    assert out == {"TZ": "UTC"}, out
    print("OK credential_without_provider_skipped")


def test_precedence_passthrough_lt_static_lt_credential():
    prov = InMemoryCredentialProvider(shared=False)
    asyncio.run(prov.put(tenant_id="t", key="k", value="from-cred"))
    spec = SandboxEnvSpec(
        passthrough=("X",),
        static={"X": "from-static"},
        credentials={"X": "k"},
    )
    out = asyncio.run(
        spec.resolve(tenant_id="t", credentials=prov, host_env={"X": "from-host"})
    )
    assert out == {"X": "from-cred"}, out  # credential wins
    # and static beats passthrough when no credential overrides
    spec2 = SandboxEnvSpec(passthrough=("X",), static={"X": "from-static"})
    out2 = asyncio.run(spec2.resolve(tenant_id="t", host_env={"X": "from-host"}))
    assert out2 == {"X": "from-static"}, out2
    print("OK precedence_passthrough_lt_static_lt_credential")


# --- factory + parser ----------------------------------------------------

def test_factory_from_env():
    env = {
        "ADK_CC_SANDBOX_ENV": "TZ=UTC,LANG=C.UTF-8",
        "ADK_CC_SANDBOX_ENV_PASSTHROUGH": "GITHUB_TOKEN, HF_TOKEN ,",
        "ADK_CC_SANDBOX_ENV_CREDENTIALS": "GITHUB_TOKEN=gh_pat",
    }
    spec = sandbox_env_spec_from_env(env)
    assert spec.static == {"TZ": "UTC", "LANG": "C.UTF-8"}, spec.static
    assert spec.passthrough == ("GITHUB_TOKEN", "HF_TOKEN"), spec.passthrough
    assert spec.credentials == {"GITHUB_TOKEN": "gh_pat"}, spec.credentials
    print("OK factory_from_env")


def test_factory_empty_when_unset():
    spec = sandbox_env_spec_from_env({})
    assert spec.is_empty()
    print("OK factory_empty_when_unset")


def test_parse_kv_json_form():
    # JSON escape hatch for values with commas/equals.
    out = _parse_kv('{"URL": "https://x/?a=1,b=2"}', what="t")
    assert out == {"URL": "https://x/?a=1,b=2"}, out
    print("OK parse_kv_json_form")


def test_parse_kv_value_with_equals():
    out = _parse_kv("TOKEN=ab==cd", what="t")  # split on FIRST =
    assert out == {"TOKEN": "ab==cd"}, out
    print("OK parse_kv_value_with_equals")


def test_parse_kv_errors():
    for bad in ("noequals", "{not json}"):
        try:
            _parse_kv(bad, what="t")
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass
    print("OK parse_kv_errors")


def main():
    test_empty_spec_is_empty_and_resolves_blank()
    test_static_literals()
    test_passthrough_present_and_absent()
    test_credentials_resolved_per_tenant()
    test_credential_missing_key_skipped()
    test_credential_without_provider_skipped()
    test_precedence_passthrough_lt_static_lt_credential()
    test_factory_from_env()
    test_factory_empty_when_unset()
    test_parse_kv_json_form()
    test_parse_kv_value_with_equals()
    test_parse_kv_errors()
    print("\nall sandbox-env tests passed")


if __name__ == "__main__":
    main()
