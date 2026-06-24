"""E2E for the five reported fixes, through the UI (real browser, no model).

  1. admin MCP/Skills tabs load for an admin (no 403 — caller-tenant scoping)
  2. fresh login lands on chat (/), and explicit sign-out returns to /
  3. chat header shows the email, not the opaque user_id
  4. sign-out shows the email form with NO token-form flash
  5. owner role: the owner row shows an 'owner' badge and can't be disabled,
     even by another admin (bob)

Run: .venv/bin/python tests/e2e_fixes_ui.py
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
PORT = 8919
BASE = f"http://127.0.0.1:{PORT}"

_passed = _failed = 0


def check(name, ok):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def _provision(iddir):
    from adk_cc.identity.store import JsonFileUserStore
    from adk_cc.identity.provider import EmailPasswordProvider
    store = JsonFileUserStore(os.path.join(iddir, "users.json"))
    p = EmailPasswordProvider(store, mode="single", global_tenant_id="acme", admin_role="admin")
    p.provision(email="alice@acme.io", password="password123", name="Alice", tenant_id="acme", roles=["owner", "admin"])
    p.provision(email="bob@acme.io", password="password123", name="Bob", tenant_id="acme", roles=["admin"])
    p.provision(email="carol@acme.io", password="password123", name="Carol", tenant_id="acme", roles=["member"])


def _login_ui(page, email):
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_selector("#email", timeout=10000)
    page.fill("#email", email)
    page.fill("#password", "password123")
    page.click("button[type=submit]")
    page.wait_for_selector('button[title="Settings"]', timeout=20000)


def main() -> int:
    if not os.path.isfile(os.path.join(REPO, "web", "dist", "index.html")):
        print("SKIP: web/dist not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    root = tempfile.mkdtemp(prefix="fixes-ui-")
    iddir = os.path.join(root, "identity")
    os.makedirs(iddir, exist_ok=True)
    _provision(iddir)

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1", "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "acme", "ADK_CC_IDENTITY_DIR": iddir,
        "ADK_CC_SERVE_UI": "1", "ADK_CC_ADMIN_PANEL": "1",
        "ADK_CC_TENANT_REGISTRY_DIR": os.path.join(root, "registry"),
        "ADK_CC_TENANT_SKILLS_DIR": os.path.join(root, "skills"),
        "ADK_CC_MODEL_REGISTRY_FILE": os.path.join(root, "models.json"),
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

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)

            # ---------- alice (owner+admin) ----------
            actx = browser.new_context(viewport={"width": 1200, "height": 850})
            a = actx.new_page()
            _login_ui(a, "alice@acme.io")

            # (2) fresh login lands on chat at /
            check("fresh login lands on chat (/)",
                  a.url.rstrip("/") == BASE and a.locator('button[title="Settings"]').count() == 1)
            # (3) header shows email, not the uuid
            check("chat header shows the email (not user_id)",
                  a.get_by_text("alice@acme.io").count() > 0)

            # (1) admin MCP tab loads — no 403/forbidden
            a.goto(BASE + "/admin/mcp", wait_until="networkidle")
            a.wait_for_selector("text=Admin", timeout=10000)
            a.wait_for_selector("button:has-text('Add')", timeout=10000)
            check("admin MCP tab loads without a forbidden error",
                  a.get_by_role("button", name="Add").count() > 0
                  and a.locator(".text-destructive").count() == 0)
            # (1) admin Skills tab loads — no 403/forbidden
            a.goto(BASE + "/admin/skills", wait_until="networkidle")
            a.wait_for_selector("text=Skills", timeout=10000)
            time.sleep(0.6)
            check("admin Skills tab loads without a forbidden error",
                  a.locator(".text-destructive").count() == 0)

            # (5) owner row shows badge + can't be disabled (alice viewing self/owner)
            a.goto(BASE + "/org", wait_until="networkidle")
            a.wait_for_selector("text=alice@acme.io", timeout=10000)
            owner_row = a.locator("li").filter(has_text="alice@acme.io")
            check("owner row shows an 'owner' badge",
                  owner_row.get_by_text("owner").count() > 0)
            check("owner's Disable button is disabled",
                  owner_row.get_by_role("button", name="Disable").is_disabled())

            # (2) explicit sign-out returns to / ; (4) email form, no token flash
            a.goto(BASE + "/", wait_until="networkidle")
            a.wait_for_selector('button[title="Settings"]', timeout=20000)
            a.click('button[title="Settings"]')
            a.get_by_text("Sign out").first.click()
            a.wait_for_selector("#email", timeout=10000)
            check("sign-out returns to / with the email form",
                  a.url.rstrip("/") == BASE and a.locator("#email").count() == 1)
            token_seen = 0
            for _ in range(12):
                token_seen = max(token_seen, a.locator("#token").count())
                time.sleep(0.08)
            check("no token-paste form flashes during sign-out", token_seen == 0)
            actx.close()

            # ---------- bob (admin, NOT owner) ----------
            bctx = browser.new_context(viewport={"width": 1200, "height": 850})
            b = bctx.new_page()
            _login_ui(b, "bob@acme.io")
            b.goto(BASE + "/org", wait_until="networkidle")
            b.wait_for_selector("text=alice@acme.io", timeout=10000)
            alice_row = b.locator("li").filter(has_text="alice@acme.io")
            check("another admin sees the owner badge on the owner",
                  alice_row.get_by_text("owner").count() > 0)
            check("another admin CANNOT disable the owner (button disabled)",
                  alice_row.get_by_role("button", name="Disable").is_disabled())
            bctx.close()
            browser.close()

        # API-level: bob disabling alice (owner) → 400
        bt = requests.post(BASE + "/auth/login",
                           json={"email": "bob@acme.io", "password": "password123"}, timeout=5).json()["access_token"]
        members = requests.get(BASE + "/orgs/members", headers={"Authorization": f"Bearer {bt}"}, timeout=5).json()["members"]
        alice_id = next(m["id"] for m in members if m["email"] == "alice@acme.io")
        r = requests.post(BASE + f"/orgs/members/{alice_id}/disable",
                          headers={"Authorization": f"Bearer {bt}"}, timeout=5)
        check("API: another admin disabling the owner → 400", r.status_code == 400)

        print(f"\nfixes UI e2e: {_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
