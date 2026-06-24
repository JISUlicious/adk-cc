"""E2E for audit log + usage summary over real HTTP (no model).

Actions (login, provision user, role change, disable, api key) get recorded;
the admin reads /orgs/audit (most-recent-first) and /orgs/usage (per-user
aggregation). Non-admins are refused.

Run: .venv/bin/python tests/e2e_audit.py
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
PORT = 8923
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
    root = tempfile.mkdtemp(prefix="audit-e2e-")
    iddir = os.path.join(root, "identity")
    os.makedirs(iddir, exist_ok=True)
    from adk_cc.identity.store import JsonFileUserStore
    from adk_cc.identity.provider import EmailPasswordProvider
    store = JsonFileUserStore(os.path.join(iddir, "users.json"))
    p = EmailPasswordProvider(store, mode="single", global_tenant_id="acme")
    p.provision(email="alice@acme.io", password="password123", name="Alice", tenant_id="acme", roles=["owner", "admin"])
    p.provision(email="carol@acme.io", password="password123", name="Carol", tenant_id="acme", roles=["member"])

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

        def login(email, pw="password123"):
            return requests.post(BASE + "/auth/login", json={"email": email, "password": pw}, timeout=5)

        at = login("alice@acme.io").json()["access_token"]  # records "login"
        ct = login("carol@acme.io").json()["access_token"]  # records "login"

        # admin actions → audited
        requests.post(BASE + "/orgs/members", headers=H(at),
                      json={"email": "dave@acme.io", "password": "password123", "role": "member"}, timeout=5)
        members = requests.get(BASE + "/orgs/members", headers=H(at), timeout=5).json()["members"]
        dave = next(m for m in members if m["email"] == "dave@acme.io")
        requests.post(BASE + f"/orgs/members/{dave['id']}/role", headers=H(at), json={"role": "admin"}, timeout=5)
        requests.post(BASE + f"/orgs/members/{dave['id']}/disable", headers=H(at), timeout=5)
        requests.post(BASE + "/auth/api-keys", headers=H(at), json={"name": "ci"}, timeout=5)

        # audit log (most-recent-first)
        ev = requests.get(BASE + "/orgs/audit", headers=H(at), timeout=5)
        check("GET /orgs/audit → 200", ev.status_code == 200)
        actions = [e["action"] for e in ev.json()["events"]]
        for want in ("login", "user.created", "member.role", "member.disabled", "apikey.created"):
            check(f"audit recorded '{want}'", want in actions)
        check("audit is most-recent-first (apikey.created before login)",
              actions.index("apikey.created") < actions.index("login"))
        # events carry actor + target
        created = next(e for e in ev.json()["events"] if e["action"] == "user.created")
        check("audit event carries actor + target",
              created["actor"] == "alice@acme.io" and created["target"] == "dave@acme.io")

        # usage summary aggregates per user
        usage = requests.get(BASE + "/orgs/usage", headers=H(at), timeout=5)
        check("GET /orgs/usage → 200", usage.status_code == 200)
        by_email = {u["email"]: u for u in usage.json()["users"]}
        check("usage lists all members (alice, carol, dave)",
              {"alice@acme.io", "carol@acme.io", "dave@acme.io"} <= set(by_email))
        check("alice has the most events (did the admin actions)",
              by_email["alice@acme.io"]["events"] >= by_email["carol@acme.io"]["events"]
              and by_email["alice@acme.io"]["events"] > 1)
        check("each active user has a last_active timestamp",
              bool(by_email["alice@acme.io"]["last_active"]))

        # non-admin refused
        check("non-admin → 403 on /orgs/audit",
              requests.get(BASE + "/orgs/audit", headers=H(ct), timeout=5).status_code == 403)
        check("non-admin → 403 on /orgs/usage",
              requests.get(BASE + "/orgs/usage", headers=H(ct), timeout=5).status_code == 403)

        print(f"\naudit e2e: {_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
