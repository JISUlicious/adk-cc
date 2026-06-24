"""E2E: usage & audit dashboards through the UI (real browser, no model).

Admin performs an action (provision a user), then opens the Audit tab (sees the
event) and the Usage tab (sees per-user activity counts).

Run: .venv/bin/python tests/e2e_usage_audit_ui.py
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
PORT = 8924
BASE = f"http://127.0.0.1:{PORT}"

_passed = _failed = 0


def check(name, ok):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    if not os.path.isfile(os.path.join(REPO, "web", "dist", "index.html")):
        print("SKIP: web/dist not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    root = tempfile.mkdtemp(prefix="usage-ui-")
    iddir = os.path.join(root, "identity")
    os.makedirs(iddir, exist_ok=True)
    from adk_cc.identity.store import JsonFileUserStore
    from adk_cc.identity.provider import EmailPasswordProvider
    store = JsonFileUserStore(os.path.join(iddir, "users.json"))
    EmailPasswordProvider(store, mode="single", global_tenant_id="acme").provision(
        email="alice@acme.io", password="password123", name="Alice", tenant_id="acme", roles=["owner", "admin"])

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1", "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "acme", "ADK_CC_IDENTITY_DIR": iddir,
        "ADK_CC_SERVE_UI": "1", "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub",
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

        # an admin action up front so the log/usage have content
        at = requests.post(BASE + "/auth/login",
                           json={"email": "alice@acme.io", "password": "password123"}, timeout=5).json()["access_token"]
        requests.post(BASE + "/orgs/members", headers={"Authorization": f"Bearer {at}"},
                      json={"email": "dave@acme.io", "password": "password123", "role": "member"}, timeout=5)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 850})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector("#email", timeout=10000)
            page.fill("#email", "alice@acme.io")
            page.fill("#password", "password123")
            page.click("button[type=submit]")
            page.wait_for_selector('button[title="Settings"]', timeout=20000)

            # Audit tab
            page.goto(BASE + "/admin/audit", wait_until="networkidle")
            page.wait_for_selector("text=Audit log", timeout=10000)
            check("Audit tab shows the user.created event",
                  page.get_by_text("user.created").count() > 0
                  and page.get_by_text("dave@acme.io").count() > 0)
            check("Audit tab shows a login event", page.get_by_text("login", exact=True).count() > 0)

            # Usage tab
            page.goto(BASE + "/admin/usage", wait_until="networkidle")
            page.wait_for_selector("text=Activity by user", timeout=10000)
            check("Usage tab lists users with activity",
                  page.get_by_text("alice@acme.io").count() > 0
                  and page.get_by_text("dave@acme.io").count() > 0)
            # alice's row should show a non-zero event count
            alice_row = page.locator("tr").filter(has_text="alice@acme.io")
            check("Usage shows alice's row", alice_row.count() > 0)

            browser.close()
        print(f"\nusage/audit UI e2e: {_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
