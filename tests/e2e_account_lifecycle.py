"""E2E for account lifecycle over real HTTP (no model).

Email change (login moves to the new address) → self-deactivate (login blocked,
admin re-enables) → self-delete (record gone, PAT dead, workspace directory
purged, email free again) → owner/last-admin refusals.

Run: .venv/bin/python tests/e2e_account_lifecycle.py
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
PORT = 8924
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


def _login(email: str, password: str):
    return requests.post(BASE + "/auth/login",
                         json={"email": email, "password": password}, timeout=5)


def main() -> int:
    root = tempfile.mkdtemp(prefix="al-e2e-")
    ws_root = tempfile.mkdtemp(prefix="al-ws-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_IDENTITY_DIR": root,
        "ADK_CC_WORKSPACE_ROOT": ws_root,
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

        at = _login("admin@local.io", "adminpass123").json()["access_token"]
        requests.post(BASE + "/orgs/members", headers=_hdr(at),
                      json={"email": "m@local.io", "password": "password123",
                            "role": "member"}, timeout=5)
        mt = _login("m@local.io", "password123").json()["access_token"]

        # --- email change -------------------------------------------------
        r = requests.post(BASE + "/auth/email", headers=_hdr(mt),
                          json={"new_email": "m2@local.io", "password": "wrong"}, timeout=5)
        check("email change with wrong password → 400", r.status_code == 400)
        r = requests.post(BASE + "/auth/email", headers=_hdr(mt),
                          json={"new_email": "M2@Local.io", "password": "password123"},
                          timeout=5)
        check("email change → normalized new address",
              r.ok and r.json()["email"] == "m2@local.io")
        check("login moves to the new address",
              _login("m2@local.io", "password123").ok
              and _login("m@local.io", "password123").status_code == 401)

        # --- deactivate → admin re-enable ----------------------------------
        mt = _login("m2@local.io", "password123").json()["access_token"]
        r = requests.post(BASE + "/auth/account/deactivate", headers=_hdr(mt),
                          json={"password": "password123"}, timeout=5)
        check("self-deactivate → 200", r.ok and r.json()["status"] == "disabled")
        check("deactivated login → 401",
              _login("m2@local.io", "password123").status_code == 401)
        members = requests.get(BASE + "/orgs/members", headers=_hdr(at),
                               timeout=5).json()["members"]
        mid = [m for m in members if m["email"] == "m2@local.io"][0]["id"]
        requests.post(BASE + f"/orgs/members/{mid}/enable", headers=_hdr(at), timeout=5)
        check("admin re-enable restores login",
              _login("m2@local.io", "password123").ok)

        # --- delete with cleanup -------------------------------------------
        login = _login("m2@local.io", "password123").json()
        mt, uid = login["access_token"], login["user"]["id"]
        pat = requests.post(BASE + "/auth/api-keys", headers=_hdr(mt),
                            json={"name": "ci"}, timeout=5).json()["token"]
        check("PAT works before deletion",
              requests.get(BASE + "/auth/me", headers=_hdr(pat), timeout=5).ok)
        ws_dir = os.path.join(ws_root, "local", uid)
        os.makedirs(ws_dir, exist_ok=True)
        open(os.path.join(ws_dir, "file.txt"), "w").write("data")

        r = requests.delete(BASE + "/auth/account", headers=_hdr(mt),
                            json={"password": "password123"}, timeout=5)
        check("self-delete → 200", r.ok and r.json()["status"] == "deleted")
        check("deleted login → 401",
              _login("m2@local.io", "password123").status_code == 401)
        check("PAT is dead after deletion",
              requests.get(BASE + "/auth/me", headers=_hdr(pat),
                           timeout=5).status_code == 401)
        check("workspace directory purged", not os.path.exists(ws_dir))
        members = requests.get(BASE + "/orgs/members", headers=_hdr(at),
                               timeout=5).json()["members"]
        check("record gone from the members list",
              all(m["email"] != "m2@local.io" for m in members))
        r = requests.post(BASE + "/orgs/members", headers=_hdr(at),
                          json={"email": "m2@local.io", "password": "password123",
                                "role": "member"}, timeout=5)
        check("email is free again after deletion", r.ok)

        # --- owner/last-admin guard ----------------------------------------
        r = requests.delete(BASE + "/auth/account", headers=_hdr(at),
                            json={"password": "adminpass123"}, timeout=5)
        check("last admin can't self-delete", r.status_code == 400)
        r = requests.post(BASE + "/auth/account/deactivate", headers=_hdr(at),
                          json={"password": "adminpass123"}, timeout=5)
        check("last admin can't self-deactivate", r.status_code == 400)

        ev = requests.get(BASE + "/orgs/audit", headers=_hdr(at), timeout=5).json()["events"]
        actions = {e["action"] for e in ev}
        check("audit has email.changed / account.deactivated / account.deleted",
              {"email.changed", "account.deactivated", "account.deleted"} <= actions)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
