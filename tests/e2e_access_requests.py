"""E2E for access-request registration over real HTTP (no model).

Single-tenant server with a bootstrap admin. Jane requests access → pending
(login hard-blocked with a distinct 403) → admin sees the queue, approves →
Jane logs in as a member. Bob requests → rejected → record gone, can re-request.
Non-admin and unauthenticated callers can't touch the queue.

Run: .venv/bin/python tests/e2e_access_requests.py
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
PORT = 8916
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
    root = tempfile.mkdtemp(prefix="req-e2e-")
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

        cfg = requests.get(BASE + "/auth/config", timeout=5).json()
        check("config: access_requests on, registration off (single mode)",
              cfg.get("access_requests") is True and cfg.get("registration") is False)

        r = requests.post(BASE + "/auth/signup",
                          json={"email": "x@y.io", "password": "password123"}, timeout=5)
        check("signup stays 403 in single mode", r.status_code == 403)

        # Jane requests access
        r = requests.post(BASE + "/auth/request-access",
                          json={"email": "jane@example.com", "password": "janepass123",
                                "name": "Jane", "note": "QA team"}, timeout=5)
        check("request-access → 200 pending, no token",
              r.status_code == 200 and r.json().get("status") == "pending"
              and "access_token" not in r.json())

        r = requests.post(BASE + "/auth/login",
                          json={"email": "jane@example.com", "password": "janepass123"}, timeout=5)
        check("pending login → 403 awaiting approval",
              r.status_code == 403 and "approval" in r.json().get("detail", "").lower())

        r = requests.post(BASE + "/auth/login",
                          json={"email": "jane@example.com", "password": "wrong-pass!"}, timeout=5)
        check("pending + wrong password → plain 401 (no status leak)", r.status_code == 401)

        # A request for an already-registered email must look identical to a new
        # one (200 pending) — no account-enumeration oracle. And it creates no
        # second record (the queue still holds exactly one).
        r = requests.post(BASE + "/auth/request-access",
                          json={"email": "jane@example.com", "password": "janepass123"}, timeout=5)
        check("duplicate request → 200 pending (no enumeration leak)",
              r.status_code == 200 and r.json().get("status") == "pending")

        check("unauthenticated queue read → 401",
              requests.get(BASE + "/orgs/requests", timeout=5).status_code == 401)

        # Admin reviews the queue
        at = requests.post(BASE + "/auth/login",
                           json={"email": "admin@local.io", "password": "adminpass123"},
                           timeout=5).json()["access_token"]
        reqs = requests.get(BASE + "/orgs/requests", headers=_hdr(at), timeout=5).json()["requests"]
        check("admin sees jane's request with note",
              len(reqs) == 1 and reqs[0]["email"] == "jane@example.com"
              and reqs[0]["note"] == "QA team")
        jane_id = reqs[0]["id"]

        members = requests.get(BASE + "/orgs/members", headers=_hdr(at), timeout=5).json()["members"]
        check("pending jane is NOT a member yet",
              all(m["email"] != "jane@example.com" for m in members))

        m = requests.post(BASE + f"/orgs/requests/{jane_id}/approve", headers=_hdr(at), timeout=5)
        check("approve → active member",
              m.status_code == 200 and m.json()["status"] == "active"
              and m.json()["roles"] == ["member"])

        r = requests.post(BASE + "/auth/login",
                          json={"email": "jane@example.com", "password": "janepass123"}, timeout=5)
        check("approved jane logs in", r.status_code == 200 and r.json().get("access_token"))
        jt = r.json()["access_token"]

        check("queue is empty after approval",
              requests.get(BASE + "/orgs/requests", headers=_hdr(at), timeout=5).json()["requests"] == [])
        check("member (non-admin) can't read the queue",
              requests.get(BASE + "/orgs/requests", headers=_hdr(jt), timeout=5).status_code == 403)

        # Bob requests → rejected → gone; can re-request
        requests.post(BASE + "/auth/request-access",
                      json={"email": "bob@example.com", "password": "bobpass1234"}, timeout=5)
        bob_id = requests.get(BASE + "/orgs/requests", headers=_hdr(at),
                              timeout=5).json()["requests"][0]["id"]
        r = requests.post(BASE + f"/orgs/requests/{bob_id}/reject", headers=_hdr(at), timeout=5)
        check("reject → 200", r.status_code == 200 and r.json().get("status") == "rejected")
        r = requests.post(BASE + "/auth/login",
                          json={"email": "bob@example.com", "password": "bobpass1234"}, timeout=5)
        check("rejected bob can't log in (record deleted)", r.status_code == 401)
        r = requests.post(BASE + "/auth/request-access",
                          json={"email": "bob@example.com", "password": "bobpass1234"}, timeout=5)
        check("rejected email can request again", r.status_code == 200)

        # audit trail captured the lifecycle
        ev = requests.get(BASE + "/orgs/audit", headers=_hdr(at), timeout=5).json()["events"]
        actions = {e["action"] for e in ev}
        check("audit has access.requested / request.approved / request.rejected",
              {"access.requested", "request.approved", "request.rejected"} <= actions)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
