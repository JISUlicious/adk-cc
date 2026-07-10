"""E2E for auth brute-force protection over real HTTP (no model).

Tiny windows via env: 3 failed logins lock the (ip, email) pair → even the
CORRECT password 429s until the lockout ages out; a different account from
the same IP is unaffected; the per-IP burst budget 429s a hammering client.

Run: .venv/bin/python tests/e2e_ratelimit.py
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
PORT = 8923
BASE = f"http://127.0.0.1:{PORT}"

_passed = _failed = 0


def check(name: str, ok: bool) -> None:
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def _login(email: str, password: str):
    return requests.post(BASE + "/auth/login",
                         json={"email": email, "password": password}, timeout=5)


def main() -> int:
    root = tempfile.mkdtemp(prefix="rl-e2e-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_IDENTITY_DIR": root,
        "ADK_CC_BOOTSTRAP_ADMIN_EMAIL": "admin@local.io",
        "ADK_CC_BOOTSTRAP_ADMIN_PASSWORD": "adminpass123",
        "ADK_CC_AUTH_LOCKOUT_THRESHOLD": "3",
        "ADK_CC_AUTH_LOCKOUT_S": "2",
        "ADK_CC_AUTH_RATELIMIT_MAX": "15",
        "ADK_CC_AUTH_RATELIMIT_WINDOW_S": "2",
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

        # seed a second account while nothing is locked
        at = _login("admin@local.io", "adminpass123").json()["access_token"]
        requests.post(BASE + "/orgs/members",
                      headers={"Authorization": f"Bearer {at}"},
                      json={"email": "m@local.io", "password": "password123",
                            "role": "member"}, timeout=5)
        time.sleep(2.1)  # fresh burst window

        # 3 failures lock the (ip, admin@) pair
        codes = [_login("admin@local.io", f"wrong-{i}").status_code for i in range(3)]
        check("wrong passwords → 401s", codes == [401, 401, 401])
        r = _login("admin@local.io", "adminpass123")
        check("locked: even the CORRECT password 429s",
              r.status_code == 429 and r.headers.get("Retry-After"))
        check("same IP, different account still logs in (pair keying)",
              _login("m@local.io", "password123").status_code == 200)

        time.sleep(2.2)  # lockout ages out
        check("after the lockout window the correct password works",
              _login("admin@local.io", "adminpass123").status_code == 200)

        # per-IP burst budget: hammer refresh with garbage until 429
        time.sleep(2.1)  # fresh window
        got429 = None
        for _ in range(20):
            resp = requests.post(BASE + "/auth/refresh",
                                 json={"refresh_token": "garbage"}, timeout=5)
            if resp.status_code == 429:
                got429 = resp
                break
        check("hammering a public auth endpoint hits the per-IP budget (429)",
              got429 is not None and got429.headers.get("Retry-After"))

        time.sleep(2.1)
        check("budget recovers after the window",
              _login("admin@local.io", "adminpass123").status_code == 200)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
