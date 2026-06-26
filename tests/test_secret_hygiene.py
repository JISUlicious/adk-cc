"""Secret hygiene (Phase 6) + on-demand sandbox env injection (Phase 5).

Proves a resolved secret value:
  - is REDACTED from tool results (in place) and model responses before any
    plugin logs/persists/delivers them,
  - never reveals via SecretStr str/repr,
and that the NoopBackend injects the session user's secrets into the exec
subprocess on demand (user-over-tenant), picking up newly-set secrets.

Model-free. Run: .venv/bin/python tests/test_secret_hygiene.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
os.environ["ADK_CC_NOOP_ACK_HOST_EXEC"] = "1"  # allow host subprocess in tests

from adk_cc.credentials import InMemoryCredentialProvider, SecretStr  # noqa: E402
from adk_cc.plugins.secret_redaction import SecretRedactionPlugin  # noqa: E402
from adk_cc.sandbox.backends.noop_backend import NoopBackend  # noqa: E402
from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig  # noqa: E402

TENANT_KEY = "temp:tenant_context"
SECRET = "s3cr3t-VALUE-abcdef"  # high-entropy-ish so substring redaction is safe


def _state(tenant="acme", user="alice"):
    return {TENANT_KEY: SimpleNamespace(tenant_id=tenant, user_id=user)}


# ---------- SecretStr ----------
def test_secretstr_never_reveals_implicitly():
    s = SecretStr(SECRET, name="MYSECRET")
    assert str(s) == "***"
    assert "MYSECRET" in repr(s) and SECRET not in repr(s)
    assert f"{s}" == "***"
    assert s.reveal() == SECRET
    assert bool(s) is True
    assert bool(SecretStr("")) is False


# ---------- redaction: tool result ----------
def test_tool_result_redacted_in_place():
    creds = InMemoryCredentialProvider(shared=False)
    asyncio.run(creds.put(tenant_id="acme", key="MYSECRET", value=SECRET, user_id="alice"))
    plugin = SecretRedactionPlugin(creds)
    tc = SimpleNamespace(state=_state())
    result = {"stdout": f"len=18 val={SECRET}", "stderr": "", "exit_code": 0}

    ret = asyncio.run(
        plugin.after_tool_callback(tool=None, tool_args={}, tool_context=tc, result=result)
    )
    assert ret is None, "must return None (mutate in place, no short-circuit)"
    blob = json.dumps(result)
    assert SECRET not in blob, f"raw secret leaked: {blob}"
    assert "‹redacted:MYSECRET›" in result["stdout"], result["stdout"]
    assert "len=18" in result["stdout"], "non-secret content preserved"


def test_base64_form_redacted():
    creds = InMemoryCredentialProvider(shared=False)
    asyncio.run(creds.put(tenant_id="acme", key="TOK", value=SECRET, user_id="alice"))
    plugin = SecretRedactionPlugin(creds)
    tc = SimpleNamespace(state=_state())
    b64 = base64.b64encode(SECRET.encode()).decode()
    result = {"stdout": f"encoded={b64}"}
    asyncio.run(
        plugin.after_tool_callback(tool=None, tool_args={}, tool_context=tc, result=result)
    )
    assert b64 not in result["stdout"], "base64 form leaked"
    assert "‹redacted:TOK›" in result["stdout"]


# ---------- redaction: model response ----------
def test_model_response_redacted():
    creds = InMemoryCredentialProvider(shared=False)
    asyncio.run(creds.put(tenant_id="acme", key="K", value=SECRET, user_id="alice"))
    plugin = SecretRedactionPlugin(creds)
    part = SimpleNamespace(text=f"the value is {SECRET}")
    resp = SimpleNamespace(content=SimpleNamespace(parts=[part]))
    cc = SimpleNamespace(state=_state())
    ret = asyncio.run(plugin.after_model_callback(callback_context=cc, llm_response=resp))
    assert ret is None
    assert SECRET not in part.text and "‹redacted:K›" in part.text


# ---------- inert when no provider / no principal ----------
def test_inert_without_provider_or_principal():
    plugin = SecretRedactionPlugin(None)
    tc = SimpleNamespace(state=_state())
    result = {"stdout": SECRET}
    asyncio.run(plugin.after_tool_callback(tool=None, tool_args={}, tool_context=tc, result=result))
    assert result["stdout"] == SECRET, "no provider → no scrub"

    creds = InMemoryCredentialProvider(shared=False)
    asyncio.run(creds.put(tenant_id="acme", key="K", value=SECRET, user_id="alice"))
    plugin2 = SecretRedactionPlugin(creds)
    tc2 = SimpleNamespace(state={})  # no principal
    result2 = {"stdout": SECRET}
    asyncio.run(plugin2.after_tool_callback(tool=None, tool_args={}, tool_context=tc2, result=result2))
    assert result2["stdout"] == SECRET, "no principal → no scrub"


# ---------- on-demand: newly-set secret is picked up after invalidate ----------
def test_on_demand_pickup():
    creds = InMemoryCredentialProvider(shared=False)
    plugin = SecretRedactionPlugin(creds)
    tc = SimpleNamespace(state=_state())

    async def go():
        # nothing set yet
        r1 = {"stdout": SECRET}
        await plugin.after_tool_callback(tool=None, tool_args={}, tool_context=tc, result=r1)
        assert r1["stdout"] == SECRET  # not yet a secret
        # user sets it mid-session
        await creds.put(tenant_id="acme", key="LATE", value=SECRET, user_id="alice")
        plugin.invalidate()  # the PUT handler signals this
        r2 = {"stdout": SECRET}
        await plugin.after_tool_callback(tool=None, tool_args={}, tool_context=tc, result=r2)
        assert "‹redacted:LATE›" in r2["stdout"], r2["stdout"]

    asyncio.run(go())


# ---------- injection: NoopBackend subprocess sees the secret (Phase 5) ----------
def test_noop_injects_user_secret_user_over_tenant():
    creds = InMemoryCredentialProvider(shared=False)
    asyncio.run(creds.put(tenant_id="acme", key="MYSECRET", value="shared-val"))
    asyncio.run(creds.put(tenant_id="acme", key="MYSECRET", value=SECRET, user_id="alice"))

    d = tempfile.mkdtemp(prefix="noop-env-")
    backend = NoopBackend()
    backend.configure_runtime_env(
        credentials=creds, tenant_id="acme", user_id="alice", ttl_s=0.0
    )

    async def go():
        res = await backend.exec(
            'echo "len=${#MYSECRET} val=$MYSECRET"',
            fs_write=FsWriteConfig(),
            network=NetworkConfig(),
            timeout_s=15,
            cwd=d,
        )
        return res

    res = asyncio.run(go())
    assert res.exit_code == 0, (res.exit_code, res.stderr)
    # alice's personal value wins over the tenant-shared one
    assert f"len={len(SECRET)}" in res.stdout, res.stdout
    assert SECRET in res.stdout, "subprocess did not receive the injected secret"

    # bob (no personal value) falls back to the tenant-shared one
    backend.configure_runtime_env(credentials=creds, tenant_id="acme", user_id="bob", ttl_s=0.0)
    res_bob = asyncio.run(go())
    assert "shared-val" in res_bob.stdout, res_bob.stdout


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__[5:]}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__[5:]}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__[5:]}: {type(e).__name__}: {e}")
    print("\nall secret-hygiene tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
