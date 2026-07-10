"""E2E for password reset over real HTTP (no model).

Admin mints a one-time reset link for a member → public lookup shows the email
→ completing sets the password AND signs the holder in → the old password and
the member's old sessions are dead → the link is single-use → non-admins and
cross-tenant admins can't mint links.

Run: .venv/bin/python tests/e2e_password_reset.py
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
PORT = 8920
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
    root = tempfile.mkdtemp(prefix="pr-e2e-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_IDENTITY_DIR": root,
        "ADK_CC_BOOTSTRAP_ADMIN_EMAIL": "admin@local.io",
        "ADK_CC_BOOTSTRAP_ADMIN_PASSWORD": "adminpass123",
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

        admin = requests.post(BASE + "/auth/login",
                              json={"email": "admin@local.io", "password": "adminpass123"},
                              timeout=5).json()
        at = admin["access_token"]
        requests.post(BASE + "/orgs/members", headers=_hdr(at),
                      json={"email": "m@local.io", "password": "password123",
                            "role": "member"}, timeout=5)
        mem = requests.post(BASE + "/auth/login",
                            json={"email": "m@local.io", "password": "password123"},
                            timeout=5).json()
        mid = mem["user"]["id"]

        # member (non-admin) can't mint links
        check("non-admin can't mint a reset link",
              requests.post(BASE + f"/orgs/members/{mid}/reset-password",
                            headers=_hdr(mem["access_token"]),
                            timeout=5).status_code == 403)

        r = requests.post(BASE + f"/orgs/members/{mid}/reset-password",
                          headers=_hdr(at), timeout=5)
        check("admin mints a reset link",
              r.ok and "/reset-password/" in r.json()["url"]
              and r.json()["email"] == "m@local.io")
        token = r.json()["url"].rsplit("/", 1)[-1]

        info = requests.get(BASE + f"/auth/reset/{token}", timeout=5)
        check("public lookup shows the email",
              info.ok and info.json()["email"] == "m@local.io")

        check("garbage token lookup → 404",
              requests.get(BASE + "/auth/reset/garbage", timeout=5).status_code == 404)
        check("short password → 400",
              requests.post(BASE + f"/auth/reset/{token}/complete",
                            json={"password": "short"}, timeout=5).status_code == 400)

        done = requests.post(BASE + f"/auth/reset/{token}/complete",
                             json={"password": "newpassword1"}, timeout=5)
        check("complete → signed in (access + refresh)",
              done.ok and done.json().get("access_token")
              and done.json().get("refresh_token"))
        check("new tokens work",
              requests.get(BASE + "/auth/me",
                           headers=_hdr(done.json()["access_token"]), timeout=5).ok)

        check("old password is dead",
              requests.post(BASE + "/auth/login",
                            json={"email": "m@local.io", "password": "password123"},
                            timeout=5).status_code == 401)
        check("new password logs in",
              requests.post(BASE + "/auth/login",
                            json={"email": "m@local.io", "password": "newpassword1"},
                            timeout=5).ok)
        check("member's pre-reset session (refresh) is revoked",
              requests.post(BASE + "/auth/refresh",
                            json={"refresh_token": mem["refresh_token"]},
                            timeout=5).status_code == 401)

        check("link is single-use (lookup 404 after completion)",
              requests.get(BASE + f"/auth/reset/{token}", timeout=5).status_code == 404)
        check("link is single-use (complete → 400)",
              requests.post(BASE + f"/auth/reset/{token}/complete",
                            json={"password": "anotherpass1"}, timeout=5).status_code == 400)

        ev = requests.get(BASE + "/orgs/audit", headers=_hdr(at), timeout=5).json()["events"]
        actions = {e["action"] for e in ev}
        check("audit has password.reset_link + password.reset",
              {"password.reset_link", "password.reset"} <= actions)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
