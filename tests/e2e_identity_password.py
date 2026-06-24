"""E2E for the in-house email+password identity, over real HTTP (no model).

Boots the FastAPI server with ADK_CC_AUTH_PASSWORD=1 and exercises the full
wire: /auth/config, signup → token → a gated API call (/list-apps), /auth/me,
wrong/right login, a rejected bogus token, JWKS, and (single mode) the bootstrap
admin login + signup-disabled. No LLM calls, so it's safe to run unthrottled.

Run: .venv/bin/python tests/e2e_identity_password.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _server(port: int, extra_env: dict):
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_SKIP_DOTENV": "1",
        "ADK_CC_API_KEY": "stub",
    })
    env.update(extra_env)
    env.pop("ADK_CC_ALLOW_NO_AUTH", None)  # we want REAL auth
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            requests.get(base + "/auth/config", timeout=2)
            return proc, base
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("server did not start")


_passed = 0
_failed = 0


def check(name: str, ok: bool):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def run_multi() -> None:
    root = tempfile.mkdtemp(prefix="ident-multi-")
    proc, base = _server(8911, {"ADK_CC_TENANCY_MODE": "multi", "ADK_CC_IDENTITY_DIR": root})
    try:
        print("multi mode (self-serve signup):")
        cfg = requests.get(base + "/auth/config", timeout=5).json()
        check("config reports password+registration, mode=multi",
              cfg.get("password") and cfg.get("registration") and cfg.get("mode") == "multi")

        # gated API rejects the unauthenticated caller
        check("/list-apps without token → 401",
              requests.get(base + "/list-apps", timeout=5).status_code == 401)

        # signup → token
        r = requests.post(base + "/auth/signup",
                          json={"email": "owner@acme.io", "password": "password123",
                                "name": "Owner", "org": "Acme Inc"}, timeout=5)
        check("signup → 200 + token", r.status_code == 200 and r.json().get("access_token"))
        body = r.json()
        tok = body["access_token"]
        check("signup user owns tenant 'acme-inc' with admin role",
              body["user"]["tenant"] == "acme-inc" and "admin" in body["user"]["roles"])

        h = {"Authorization": f"Bearer {tok}"}
        check("issued token unlocks /list-apps (200)",
              requests.get(base + "/list-apps", headers=h, timeout=5).status_code == 200)

        me = requests.get(base + "/auth/me", headers=h, timeout=5)
        check("/auth/me reflects tenant + roles",
              me.status_code == 200 and me.json()["tenant"] == "acme-inc"
              and "admin" in me.json()["roles"])

        # duplicate email rejected
        dup = requests.post(base + "/auth/signup",
                            json={"email": "owner@acme.io", "password": "password123"}, timeout=5)
        check("duplicate-email signup → 400", dup.status_code == 400)

        # login: wrong then right
        check("login wrong password → 401",
              requests.post(base + "/auth/login",
                            json={"email": "owner@acme.io", "password": "nope"},
                            timeout=5).status_code == 401)
        lr = requests.post(base + "/auth/login",
                           json={"email": "owner@acme.io", "password": "password123"}, timeout=5)
        check("login correct password → 200 + token",
              lr.status_code == 200 and lr.json().get("access_token"))

        # a forged token is rejected by the JWKS validator
        check("bogus token → 401",
              requests.get(base + "/list-apps",
                           headers={"Authorization": "Bearer not.a.jwt"},
                           timeout=5).status_code == 401)

        jwks = requests.get(base + "/.well-known/jwks.json", timeout=5).json()
        check("JWKS endpoint serves a key", bool(jwks.get("keys")))
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


def run_single() -> None:
    root = tempfile.mkdtemp(prefix="ident-single-")
    proc, base = _server(8912, {
        "ADK_CC_TENANCY_MODE": "single", "ADK_CC_IDENTITY_DIR": root,
        "ADK_CC_BOOTSTRAP_ADMIN_EMAIL": "admin@corp.io",
        "ADK_CC_BOOTSTRAP_ADMIN_PASSWORD": "password123",
        "ADK_CC_GLOBAL_TENANT_ID": "local",
    })
    try:
        print("single mode (admin-provisioned, no signup):")
        cfg = requests.get(base + "/auth/config", timeout=5).json()
        check("config reports registration disabled, mode=single",
              cfg.get("password") and cfg.get("registration") is False
              and cfg.get("mode") == "single")
        check("self-signup → 403",
              requests.post(base + "/auth/signup",
                            json={"email": "x@y.io", "password": "password123"},
                            timeout=5).status_code == 403)
        lr = requests.post(base + "/auth/login",
                           json={"email": "admin@corp.io", "password": "password123"}, timeout=5)
        check("bootstrapped admin logs in → 200", lr.status_code == 200)
        check("bootstrapped admin is on the global tenant + admin role",
              lr.status_code == 200 and lr.json()["user"]["tenant"] == "local"
              and "admin" in lr.json()["user"]["roles"])
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    run_multi()
    run_single()
    print(f"\nidentity e2e: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
