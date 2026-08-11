"""#126 P0: identity scoping follows the PRINCIPAL, never the client id.

Three layers pinned:
  1. resolve_default_tenant scopes by the auth principal's user_id when an
     auth context exists (a spoofed path user_id no longer selects the
     victim's workspace/memory/wiki stores), and warns on mismatch.
  2. /api/turns 403s an authenticated request whose body userId differs
     from the principal — request-time ownership, independent of
     ADK_CC_AUTHZ (which covers /apps/* only). Proven against a REAL
     server with the BearerTokenExtractor.
  3. The no-auth dev flow is unchanged (passed user_id still scopes).

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_principal_scoping.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

import requests

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def resolver_checks() -> None:
    from adk_cc.service.auth import set_auth_context
    from adk_cc.service.tenancy import resolve_default_tenant

    # No auth context (dev): passed id scopes, unchanged.
    ctx = resolve_default_tenant("alice", default_root="/tmp/x")
    check("no-auth: passed user_id scopes", ctx.user_id == "alice")

    # Auth context present: principal wins over a spoofed id.
    token = set_auth_context("bob", "acme")
    try:
        ctx = resolve_default_tenant("alice", default_root="/tmp/x")
        check("authed: PRINCIPAL scopes, spoofed id ignored",
              ctx.user_id == "bob" and ctx.tenant_id == "acme",
              (ctx.user_id, ctx.tenant_id))
        ctx2 = resolve_default_tenant("bob", default_root="/tmp/x")
        check("authed: matching id unchanged", ctx2.user_id == "bob")
    finally:
        from adk_cc.service.auth import _AUTH_CTX
        _AUTH_CTX.reset(token)


def route_checks() -> None:
    data = tempfile.mkdtemp(prefix="pscope-")
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("ADK_CC_SANDBOX", "ADK_CC_DESKTOP",
                         "ADK_CC_WORKSPACE_ROOT", "ADK_CC_AUTHZ")):
            env.pop(k)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_API_KEY": "stub", "ADK_CC_DATA_DIR": data,
        "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_WORKSPACE_ROOT": tempfile.mkdtemp(prefix="pscope-ws-"),
        # Real bearer auth: two users, no AUTHZ layer — the exact config
        # where the leak lived.
        "ADK_CC_AUTH_TOKENS": "tok-alice=alice:local,tok-bob=bob:local",
        "PYTHONPATH": str(REPO / "agents"),
    })
    proc = subprocess.Popen(
        [str(REPO / ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", "8955"],
        cwd=str(REPO), env=env,
        stdout=open(os.path.join(data, "server.log"), "w"),
        stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:8955"
    try:
        for _ in range(120):
            try:
                if requests.get(base + "/list-apps",
                                headers={"Authorization": "Bearer tok-alice"},
                                timeout=2).ok:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)

        ha = {"Authorization": "Bearer tok-alice"}
        # alice creates HER session (ADK path route).
        requests.post(f"{base}/apps/adk_cc/users/alice/sessions/s-a",
                      json={}, headers=ha, timeout=30)

        # Spoof: alice starts a turn claiming to be bob → 403.
        r = requests.post(f"{base}/api/turns", headers=ha, timeout=30, json={
            "appName": "adk_cc", "userId": "bob", "sessionId": "s-a",
            "newMessage": {"role": "user", "parts": [{"text": "hi"}]}})
        check("authed spoofed userId → 403", r.status_code == 403,
              (r.status_code, r.text[:150]))

        # Matching id passes ownership (may fail later for other reasons,
        # but NOT with 403).
        r2 = requests.post(f"{base}/api/turns", headers=ha, timeout=30, json={
            "appName": "adk_cc", "userId": "alice", "sessionId": "s-a",
            "newMessage": {"role": "user", "parts": [{"text": "hi"}]}})
        check("authed matching userId is not blocked by ownership",
              r2.status_code != 403, r2.status_code)

        # Boot warning fires when memory is on without AUTHZ — separate boot.
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()


def main() -> int:
    resolver_checks()
    route_checks()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
