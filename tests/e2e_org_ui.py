"""E2E: org/team management through the UI (real browser, no model).

Owner signs up → opens the Team page → creates an invite → an invitee opens the
invite link in a SEPARATE browser context and joins by setting a password →
owner sees 2 members → owner disables the member. Exercises the public
accept-invite page (outside AuthGate) and the admin /orgs/* actions.

Run: .venv/bin/python tests/e2e_org_ui.py
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


def main() -> int:
    if not os.path.isfile(os.path.join(REPO, "web", "dist", "index.html")):
        print("SKIP: web/dist not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    root = tempfile.mkdtemp(prefix="org-ui-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1", "ADK_CC_TENANCY_MODE": "multi",
        "ADK_CC_IDENTITY_DIR": root, "ADK_CC_SERVE_UI": "1",
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

            # --- owner signs up ---
            owner_ctx = browser.new_context()
            owner = owner_ctx.new_page()
            owner.goto(BASE + "/", wait_until="networkidle")
            owner.wait_for_selector("#email", timeout=10000)
            owner.get_by_text("Create one").first.click()
            owner.wait_for_selector("#org", timeout=5000)
            owner.fill("#email", "boss@acme.io")
            owner.fill("#password", "password123")
            owner.fill("#org", "Acme")
            owner.click("button[type=submit]")
            owner.wait_for_selector('button[title="Settings"]', timeout=20000)

            # --- owner opens the Team page (1 member: themselves) ---
            owner.goto(BASE + "/org", wait_until="networkidle")
            owner.wait_for_selector("text=boss@acme.io", timeout=10000)
            members = owner.locator("section").filter(has_text="Members").locator("li")
            check("Team page lists the owner as the only member", members.count() == 1)

            # --- create an invite, grab the link ---
            owner.fill("input[type=email]", "invitee@acme.io")
            owner.get_by_role("button", name="Create invite").click()
            owner.wait_for_selector("code", timeout=10000)
            invite_url = owner.locator("code").first.inner_text().strip()
            check("invite link generated", "/invite/" in invite_url)

            # --- invitee accepts in a SEPARATE context (no shared token) ---
            invitee_ctx = browser.new_context()
            invitee = invitee_ctx.new_page()
            invitee.goto(invite_url, wait_until="networkidle")
            invitee.wait_for_selector("#password", timeout=10000)
            check("accept page shows the org to join",
                  invitee.get_by_text("Join Acme").count() > 0)
            invitee.fill("#password", "password123")
            invitee.fill("#name", "Invited User")
            invitee.get_by_role("button", name="Join").click()
            invitee.wait_for_selector('button[title="Settings"]', timeout=20000)
            check("invitee joined and entered the app",
                  invitee.locator("#password").count() == 0)
            tok = invitee.evaluate("() => localStorage.getItem('adk_cc.token')")
            who = requests.get(BASE + "/auth/me",
                               headers={"Authorization": f"Bearer {tok}"}, timeout=5).json()
            check("invitee is a member of tenant 'acme'", who.get("tenant") == "acme")

            # --- owner refreshes: now 2 members; disable the invitee ---
            owner.goto(BASE + "/org", wait_until="networkidle")
            owner.wait_for_selector("text=invitee@acme.io", timeout=10000)
            members2 = owner.locator("section").filter(has_text="Members").locator("li")
            check("owner now sees 2 members", members2.count() == 2)

            row = owner.locator("li").filter(has_text="invitee@acme.io")
            row.get_by_role("button", name="Disable").click()
            owner.wait_for_selector("li:has-text('invitee@acme.io'):has-text('disabled')", timeout=10000)
            check("owner disabled the member (row shows 'disabled')",
                  owner.locator("li").filter(has_text="invitee@acme.io")
                       .filter(has_text="disabled").count() > 0)

            browser.close()
        print(f"\norg UI e2e: {_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
