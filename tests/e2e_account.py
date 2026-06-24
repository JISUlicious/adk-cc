"""E2E for account self-service over real HTTP (no model).

Profile read/update, change password (then login with the new one), and
personal access tokens: create → use as Bearer → revoke → rejected.

Run: .venv/bin/python tests/e2e_account.py
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
PORT = 8920
BASE = f"http://127.0.0.1:{PORT}"

_passed = _failed = 0


def check(name, ok):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def H(t):
    return {"Authorization": f"Bearer {t}"}


def main() -> int:
    root = tempfile.mkdtemp(prefix="acct-e2e-")
    iddir = os.path.join(root, "identity")
    os.makedirs(iddir, exist_ok=True)
    from adk_cc.identity.store import JsonFileUserStore
    from adk_cc.identity.provider import EmailPasswordProvider
    store = JsonFileUserStore(os.path.join(iddir, "users.json"))
    EmailPasswordProvider(store, mode="single", global_tenant_id="acme").provision(
        email="alice@acme.io", password="password123", name="Alice", tenant_id="acme", roles=["admin"])

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1", "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "acme", "ADK_CC_IDENTITY_DIR": iddir,
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub",
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

        def login(pw):
            return requests.post(BASE + "/auth/login",
                                 json={"email": "alice@acme.io", "password": pw}, timeout=5)

        tok = login("password123").json()["access_token"]

        # profile read + update
        me = requests.get(BASE + "/auth/me", headers=H(tok), timeout=5).json()
        check("/auth/me returns email + name", me.get("email") == "alice@acme.io" and me.get("name") == "Alice")
        up = requests.patch(BASE + "/auth/profile", headers=H(tok), json={"name": "Alice A."}, timeout=5)
        check("update profile name → 200 + reflected",
              up.status_code == 200 and up.json().get("name") == "Alice A.")

        # change password: wrong current → 400; correct → 200; old fails, new works
        check("change password with wrong current → 400",
              requests.post(BASE + "/auth/password", headers=H(tok),
                            json={"current_password": "nope", "new_password": "newpassword1"},
                            timeout=5).status_code == 400)
        check("change password too short → 400",
              requests.post(BASE + "/auth/password", headers=H(tok),
                            json={"current_password": "password123", "new_password": "short"},
                            timeout=5).status_code == 400)
        check("change password → 200",
              requests.post(BASE + "/auth/password", headers=H(tok),
                            json={"current_password": "password123", "new_password": "newpassword1"},
                            timeout=5).status_code == 200)
        check("old password no longer works (401)", login("password123").status_code == 401)
        check("new password works (200)", login("newpassword1").status_code == 200)
        tok = login("newpassword1").json()["access_token"]

        # API keys (PATs): create → use → list → revoke → rejected
        ck = requests.post(BASE + "/auth/api-keys", headers=H(tok), json={"name": "ci"}, timeout=5)
        check("create api key → 200 + token", ck.status_code == 200 and ck.json().get("token"))
        pat = ck.json()["token"]
        key_id = ck.json()["id"]
        check("PAT authorizes a gated API call",
              requests.get(BASE + "/list-apps", headers=H(pat), timeout=5).status_code == 200)
        lst = requests.get(BASE + "/auth/api-keys", headers=H(tok), timeout=5).json()["keys"]
        check("api key listed (name 'ci', not revoked)",
              any(k["name"] == "ci" and not k["revoked"] for k in lst))
        rv = requests.delete(BASE + f"/auth/api-keys/{key_id}", headers=H(tok), timeout=5)
        check("revoke api key → 200", rv.status_code == 200)
        check("revoked PAT is rejected (401)",
              requests.get(BASE + "/list-apps", headers=H(pat), timeout=5).status_code == 401)
        check("revoked key no longer listed",
              all(k["id"] != key_id for k in requests.get(BASE + "/auth/api-keys", headers=H(tok), timeout=5).json()["keys"]))

        print(f"\naccount e2e: {_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
