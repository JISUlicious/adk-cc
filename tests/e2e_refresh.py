"""E2E for refresh tokens over real HTTP (no model).

Server runs with a 2-second access TTL: login yields access+refresh → access
genuinely expires → gated API 401s → POST /auth/refresh returns a working pair
(rotated) → replaying the OLD refresh kills the chain → logout revokes → a
disabled member's refresh dies.

Run: .venv/bin/python tests/e2e_refresh.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8918
BASE = f"http://127.0.0.1:{PORT}"

_passed = _failed = 0


def check(name: str, ok: bool) -> None:
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def _hdr(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def main() -> int:
    root = tempfile.mkdtemp(prefix="rt-e2e-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_IDENTITY_DIR": root,
        "ADK_CC_BOOTSTRAP_ADMIN_EMAIL": "admin@local.io",
        "ADK_CC_BOOTSTRAP_ADMIN_PASSWORD": "adminpass123",
        "ADK_CC_AUTH_TOKEN_TTL_S": "2",  # access dies fast — forces the refresh path
        "ADK_CC_SKIP_DOTENV": "1",
        "ADK_CC_API_KEY": "stub",
    })
    env.pop("ADK_CC_ALLOW_NO_AUTH", None)
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                if requests.get(BASE + "/auth/config", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)

        login = requests.post(BASE + "/auth/login",
                              json={"email": "admin@local.io", "password": "adminpass123"},
                              timeout=5).json()
        check("login returns access + refresh tokens",
              bool(login.get("access_token")) and bool(login.get("refresh_token")))
        access1, refresh1 = login["access_token"], login["refresh_token"]

        check("fresh access token works",
              requests.get(BASE + "/auth/me", headers=_hdr(access1), timeout=5).ok)

        time.sleep(3)  # let the 2s access token genuinely expire
        check("expired access token → 401",
              requests.get(BASE + "/auth/me", headers=_hdr(access1), timeout=5).status_code == 401)

        r = requests.post(BASE + "/auth/refresh",
                          json={"refresh_token": refresh1}, timeout=5)
        check("refresh → 200 with a NEW rotated pair",
              r.ok and r.json().get("access_token")
              and r.json().get("refresh_token") not in (None, "", refresh1))
        access2, refresh2 = r.json()["access_token"], r.json()["refresh_token"]

        check("refreshed access token works",
              requests.get(BASE + "/auth/me", headers=_hdr(access2), timeout=5).ok)

        # replaying the OLD refresh token = theft signal → chain dies
        check("old refresh replay → 401",
              requests.post(BASE + "/auth/refresh",
                            json={"refresh_token": refresh1}, timeout=5).status_code == 401)
        check("reuse kills the CURRENT refresh too (chain revocation)",
              requests.post(BASE + "/auth/refresh",
                            json={"refresh_token": refresh2}, timeout=5).status_code == 401)

        # logout revokes
        login2 = requests.post(BASE + "/auth/login",
                               json={"email": "admin@local.io", "password": "adminpass123"},
                               timeout=5).json()
        r = requests.post(BASE + "/auth/logout",
                          json={"refresh_token": login2["refresh_token"]}, timeout=5)
        check("logout → 204", r.status_code == 204)
        check("logged-out refresh token is dead",
              requests.post(BASE + "/auth/refresh",
                            json={"refresh_token": login2["refresh_token"]},
                            timeout=5).status_code == 401)

        # disabled member's refresh dies
        admin = requests.post(BASE + "/auth/login",
                              json={"email": "admin@local.io", "password": "adminpass123"},
                              timeout=5).json()
        requests.post(BASE + "/orgs/members", headers=_hdr(admin["access_token"]),
                      json={"email": "m@local.io", "password": "password123",
                            "role": "member"}, timeout=5)
        mem = requests.post(BASE + "/auth/login",
                            json={"email": "m@local.io", "password": "password123"},
                            timeout=5).json()
        mid = mem["user"]["id"]
        requests.post(BASE + f"/orgs/members/{mid}/disable",
                      headers=_hdr(admin["access_token"]), timeout=5)
        check("disabled member's refresh token is dead",
              requests.post(BASE + "/auth/refresh",
                            json={"refresh_token": mem["refresh_token"]},
                            timeout=5).status_code == 401)

        check("garbage refresh → 401",
              requests.post(BASE + "/auth/refresh",
                            json={"refresh_token": "garbage"}, timeout=5).status_code == 401)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
