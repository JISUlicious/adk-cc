"""E2E: admin Users tab (Phase 5) through the UI (real browser, no model).

Admin opens /admin/users, provisions a new user with a password + role, sees it
listed, the new user can log in, then the admin disables them (login blocked).
Owner row is shown protected.

Run: .venv/bin/python tests/e2e_users_admin_ui.py
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
PORT = 8922
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

    root = tempfile.mkdtemp(prefix="users-ui-")
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

        def login(email, pw):
            return requests.post(BASE + "/auth/login", json={"email": email, "password": pw}, timeout=5)

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

            # open the admin Users tab
            page.goto(BASE + "/admin/users", wait_until="networkidle")
            page.wait_for_selector("text=Create a user", timeout=10000)
            check("admin Users tab loads (owner listed, protected)",
                  page.locator("li").filter(has_text="alice@acme.io").get_by_text("owner").count() > 0)

            # provision a new user
            page.fill('input[placeholder="user@acme.io"]', "dave@acme.io")
            page.fill('input[placeholder="min 8 chars"]', "password123")
            page.locator('select').first.select_option("member")
            page.get_by_role("button", name="Create", exact=True).click()
            page.wait_for_selector("text=Created dave@acme.io.", timeout=10000)
            check("created user appears in the list",
                  page.locator("li").filter(has_text="dave@acme.io").count() > 0)
            check("provisioned user can log in", login("dave@acme.io", "password123").status_code == 200)

            # disable the new user via the tab → login blocked
            page.locator("li").filter(has_text="dave@acme.io").get_by_role("button", name="Disable").click()
            page.wait_for_selector("li:has-text('dave@acme.io'):has-text('disabled')", timeout=10000)
            check("disabled user shows 'disabled' and can't log in",
                  page.locator("li").filter(has_text="dave@acme.io").filter(has_text="disabled").count() > 0
                  and login("dave@acme.io", "password123").status_code == 401)

            browser.close()

        # API: duplicate email provision → 400
        at = login("alice@acme.io", "password123").json()["access_token"]
        dup = requests.post(BASE + "/orgs/members", headers={"Authorization": f"Bearer {at}"},
                            json={"email": "dave@acme.io", "password": "password123", "role": "member"}, timeout=5)
        check("API: provisioning a duplicate email → 400", dup.status_code == 400)

        print(f"\nusers admin UI e2e: {_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
