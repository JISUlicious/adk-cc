"""E2E for the pass-A security fixes over real HTTP (no model).

Covers: (#1) an admin can't mint a reset link for the owner or a peer admin;
(#2) a disabled account can't complete an outstanding reset link; (#6) with
ADK_CC_TRUST_PROXY the lockout keys on X-Forwarded-For (a victim isn't locked
from a different client IP); (#9) change-password returns a working token pair
and revokes the OLD refresh; (#10) the enable endpoint can't activate a pending
request.

Run: .venv/bin/python tests/e2e_security_hardening.py
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


def _start(port: int, extra: dict):
    root = tempfile.mkdtemp(prefix="sec-e2e-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_IDENTITY_DIR": root,
        "ADK_CC_SKIP_DOTENV": "1",
        "ADK_CC_API_KEY": "stub",
    })
    env.update(extra)
    env.pop("ADK_CC_ALLOW_NO_AUTH", None)
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            if requests.get(base + "/auth/config", timeout=2).ok:
                return proc, base
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("server did not start")


def _reset_hierarchy_and_disabled(base):
    # multi mode: owner signs up owning 'acme', provisions two admins + a member
    owner = requests.post(base + "/auth/signup",
                          json={"email": "owner@acme.io", "password": "password123",
                                "org": "Acme"}, timeout=5).json()
    ot = owner["access_token"]
    owner_id = owner["user"]["id"]
    ids = {}
    for email, role in (("a1@acme.io", "admin"), ("a2@acme.io", "admin"),
                        ("m@acme.io", "member")):
        m = requests.post(base + "/orgs/members", headers=_hdr(ot),
                          json={"email": email, "password": "password123", "role": role},
                          timeout=5).json()
        ids[email] = m["id"]
    a1 = requests.post(base + "/auth/login",
                       json={"email": "a1@acme.io", "password": "password123"},
                       timeout=5).json()["access_token"]

    # #1 admin cannot reset the owner or a peer admin
    check("admin → reset owner is refused (400)",
          requests.post(base + f"/orgs/members/{owner_id}/reset-password",
                        headers=_hdr(a1), timeout=5).status_code == 400)
    check("admin → reset peer admin is refused (400)",
          requests.post(base + f"/orgs/members/{ids['a2@acme.io']}/reset-password",
                        headers=_hdr(a1), timeout=5).status_code == 400)
    r = requests.post(base + f"/orgs/members/{ids['m@acme.io']}/reset-password",
                      headers=_hdr(a1), timeout=5)
    check("admin → reset a member is allowed", r.status_code == 200)
    check("owner → reset an admin is allowed",
          requests.post(base + f"/orgs/members/{ids['a1@acme.io']}/reset-password",
                        headers=_hdr(ot), timeout=5).status_code == 200)

    # #2 a disabled account can't complete its outstanding reset link
    token = r.json()["url"].rsplit("/", 1)[-1]
    requests.post(base + f"/orgs/members/{ids['m@acme.io']}/disable",
                  headers=_hdr(ot), timeout=5)
    check("disabled: reset lookup now 404s",
          requests.get(base + f"/auth/reset/{token}", timeout=5).status_code == 404)
    check("disabled: completing the reset is refused (400)",
          requests.post(base + f"/auth/reset/{token}/complete",
                        json={"password": "newpassword1"}, timeout=5).status_code == 400)
    check("disabled account still can't log in (reset didn't re-open it)",
          requests.post(base + "/auth/login",
                        json={"email": "m@acme.io", "password": "newpassword1"},
                        timeout=5).status_code == 401)

    # #9 change-password returns a working pair and kills the old refresh
    login = requests.post(base + "/auth/login",
                          json={"email": "a2@acme.io", "password": "password123"},
                          timeout=5).json()
    old_refresh = login["refresh_token"]
    cp = requests.post(base + "/auth/password", headers=_hdr(login["access_token"]),
                       json={"current_password": "password123",
                             "new_password": "brandnew123"}, timeout=5)
    check("change-password returns a fresh token pair",
          cp.status_code == 200 and cp.json().get("access_token")
          and cp.json().get("refresh_token"))
    check("the returned session still works",
          requests.get(base + "/auth/me",
                       headers=_hdr(cp.json()["access_token"]), timeout=5).ok
          and requests.post(base + "/auth/refresh",
                            json={"refresh_token": cp.json()["refresh_token"]},
                            timeout=5).ok)
    check("the OLD refresh token was revoked",
          requests.post(base + "/auth/refresh",
                        json={"refresh_token": old_refresh}, timeout=5).status_code == 401)


def _proxy_and_pending(base):
    at = requests.post(base + "/auth/login",
                       json={"email": "admin@local.io", "password": "adminpass123"},
                       headers={"X-Forwarded-For": "1.2.3.4"}, timeout=5).json()["access_token"]

    # #10 enable can't activate a pending access request (single mode only)
    requests.post(base + "/auth/request-access",
                  json={"email": "req@local.io", "password": "password123"},
                  headers={"X-Forwarded-For": "1.2.3.4"}, timeout=5)
    reqs = requests.get(base + "/orgs/requests", headers=_hdr(at), timeout=5).json()["requests"]
    check("a pending request is in the queue", len(reqs) == 1)
    rid = reqs[0]["id"]
    check("enable on a pending request is refused (400)",
          requests.post(base + f"/orgs/members/{rid}/enable",
                        headers=_hdr(at), timeout=5).status_code == 400)
    check("the request is still pending after the refused enable",
          len(requests.get(base + "/orgs/requests", headers=_hdr(at),
                           timeout=5).json()["requests"]) == 1)

    # #6 with ADK_CC_TRUST_PROXY the lockout keys on X-Forwarded-For, so an
    # attacker from one IP can't lock the victim's login from another IP.
    def login(pw, xff):
        return requests.post(base + "/auth/login",
                             json={"email": "admin@local.io", "password": pw},
                             headers={"X-Forwarded-For": xff}, timeout=5)

    for _ in range(3):
        login("wrong", "9.9.9.9")  # attacker IP
    check("attacker IP is locked after threshold",
          login("adminpass123", "9.9.9.9").status_code == 429)
    check("victim's correct login from a DIFFERENT IP still works",
          login("adminpass123", "5.6.7.8").status_code == 200)


def main() -> int:
    # multi-mode server for the reset-hierarchy / password / pending checks
    proc, base = _start(8929, {"ADK_CC_TENANCY_MODE": "multi"})
    try:
        _reset_hierarchy_and_disabled(base)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    # single-mode server with trusted proxy for the lockout-keying check
    proc, base = _start(8930, {
        "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_TRUST_PROXY": "1",
        "ADK_CC_BOOTSTRAP_ADMIN_EMAIL": "admin@local.io",
        "ADK_CC_BOOTSTRAP_ADMIN_PASSWORD": "adminpass123",
        "ADK_CC_AUTH_LOCKOUT_THRESHOLD": "3",
        "ADK_CC_AUTH_LOCKOUT_S": "60",
    })
    try:
        _proxy_and_pending(base)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
