"""E2E for org/team management over real HTTP (no model).

Owner signs up → invites a member → member accepts via the public invite API →
member joins the SAME tenant → owner lists/role-changes/disables members →
non-admin and cross-tenant access are refused → revoked invites stop working.

Run: .venv/bin/python tests/e2e_org.py
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
PORT = 8915
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
    root = tempfile.mkdtemp(prefix="org-e2e-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_TENANCY_MODE": "multi",
        "ADK_CC_IDENTITY_DIR": root,
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

        # owner signs up (owns tenant "acme")
        owner = requests.post(BASE + "/auth/signup",
                              json={"email": "owner@acme.io", "password": "password123",
                                    "org": "Acme"}, timeout=5).json()
        ot = owner["access_token"]
        check("owner signup → admin of tenant 'acme'",
              owner["user"]["tenant"] == "acme" and "admin" in owner["user"]["roles"])

        # owner invites a member
        inv = requests.post(BASE + "/orgs/invites", headers=_hdr(ot),
                            json={"email": "member@acme.io", "role": "member"}, timeout=5)
        check("create invite → 200 with token + url",
              inv.status_code == 200 and inv.json().get("token") and "/invite/" in inv.json().get("url", ""))
        token = inv.json()["token"]

        # public invite lookup (no auth)
        info = requests.get(BASE + f"/auth/invite/{token}", timeout=5)
        check("public invite lookup shows email + org",
              info.status_code == 200 and info.json()["org"] == "acme"
              and info.json()["email"] == "member@acme.io")

        # accept the invite (public) → member joins acme
        acc = requests.post(BASE + f"/auth/invite/{token}/accept",
                            json={"password": "password123", "name": "Mem"}, timeout=5)
        check("accept invite → 200 + token, member on tenant 'acme'",
              acc.status_code == 200 and acc.json()["user"]["tenant"] == "acme"
              and "member" in acc.json()["user"]["roles"])
        mt = acc.json()["access_token"]
        member_id = acc.json()["user"]["id"]

        check("member's token works on a gated API",
              requests.get(BASE + "/list-apps", headers=_hdr(mt), timeout=5).status_code == 200)

        # owner now sees 2 members
        members = requests.get(BASE + "/orgs/members", headers=_hdr(ot), timeout=5)
        check("owner lists 2 members",
              members.status_code == 200 and len(members.json()["members"]) == 2)

        # the member (non-admin) cannot list members
        check("non-admin → 403 on /orgs/members",
              requests.get(BASE + "/orgs/members", headers=_hdr(mt), timeout=5).status_code == 403)

        # reused invite token is refused
        check("reusing an accepted invite → 400",
              requests.post(BASE + f"/auth/invite/{token}/accept",
                            json={"password": "password123"}, timeout=5).status_code == 400)

        # promote member → admin, then disable → their login is blocked
        check("promote member to admin → 200",
              requests.post(BASE + f"/orgs/members/{member_id}/role", headers=_hdr(ot),
                            json={"role": "admin"}, timeout=5).status_code == 200)
        check("disable member → 200",
              requests.post(BASE + f"/orgs/members/{member_id}/disable", headers=_hdr(ot),
                            timeout=5).status_code == 200)
        check("disabled member can no longer log in (401)",
              requests.post(BASE + "/auth/login",
                            json={"email": "member@acme.io", "password": "password123"},
                            timeout=5).status_code == 401)

        # cross-tenant isolation: a second org's admin sees only its own members
        bob = requests.post(BASE + "/auth/signup",
                            json={"email": "bob@beta.io", "password": "password123",
                                  "org": "Beta"}, timeout=5).json()
        bt = bob["access_token"]
        bmembers = requests.get(BASE + "/orgs/members", headers=_hdr(bt), timeout=5).json()
        check("cross-tenant: beta admin sees only beta members",
              len(bmembers["members"]) == 1 and bmembers["members"][0]["email"] == "bob@beta.io")
        check("cross-tenant: beta admin cannot touch an acme member (404)",
              requests.post(BASE + f"/orgs/members/{member_id}/enable", headers=_hdr(bt),
                            timeout=5).status_code == 404)

        # revoke an invite → public lookup 404s
        inv2 = requests.post(BASE + "/orgs/invites", headers=_hdr(ot),
                            json={"email": "later@acme.io"}, timeout=5).json()
        requests.delete(BASE + f"/orgs/invites/{inv2['token']}", headers=_hdr(ot), timeout=5)
        check("revoked invite → public lookup 404",
              requests.get(BASE + f"/auth/invite/{inv2['token']}", timeout=5).status_code == 404)

        print(f"\norg e2e: {_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
