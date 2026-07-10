"""E2E: the password-reset journey driven entirely through the UI (real browser).

Admin opens Team → clicks the key button on a member row → a one-time link
appears → the member opens that link (fresh page), sets a new password →
lands straight in the app (auto-login). The old password then fails at the
login form; the link is dead on second use.

Model-free — no chat messages are sent, so no LLM calls.
Run: .venv/bin/python tests/e2e_reset_ui.py
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
SHOT_DIR = os.environ.get("SHOT_DIR", "/tmp")
PORT = 8921
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
    from playwright.sync_api import sync_playwright

    root = tempfile.mkdtemp(prefix="ui-pr-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_SERVE_UI": "1",
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

        # seed a member via the API (the UI journey under test is the reset)
        at = requests.post(BASE + "/auth/login",
                           json={"email": "admin@local.io", "password": "adminpass123"},
                           timeout=5).json()["access_token"]
        requests.post(BASE + "/orgs/members",
                      headers={"Authorization": f"Bearer {at}"},
                      json={"email": "m@local.io", "password": "password123",
                            "role": "member"}, timeout=5)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)

            # --- admin mints the link in Team --------------------------------
            admin = browser.new_page(viewport={"width": 1100, "height": 800})
            admin.goto(BASE + "/", wait_until="networkidle")
            admin.wait_for_selector("#email", timeout=15000)
            admin.fill("#email", "admin@local.io")
            admin.fill("#password", "adminpass123")
            admin.get_by_role("button", name="Sign in").click()
            admin.wait_for_selector('button[title="Settings"]', timeout=20000)
            admin.goto(BASE + "/org", wait_until="networkidle")
            admin.wait_for_selector("text=m@local.io", timeout=10000)

            row = admin.locator("li", has_text="m@local.io")
            row.locator('button[title="Create a one-time password-reset link"]').click()
            admin.wait_for_selector("text=One-time password-reset link", timeout=10000)
            url = admin.locator("code", has_text="/reset-password/").inner_text()
            check("admin minted a visible one-time link",
                  url.startswith("http") and "/reset-password/" in url)
            admin.screenshot(path=f"{SHOT_DIR}/pr_admin_link.png")
            admin.close()

            # --- member opens the link and resets ----------------------------
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            page.goto(url, wait_until="networkidle")
            page.wait_for_selector("text=Reset your password", timeout=10000)
            check("reset page shows the member's email",
                  page.get_by_text("m@local.io").count() > 0)
            page.fill("#password", "brandnewpass1")
            page.fill("#confirm", "different")
            page.get_by_role("button", name="Reset password").click()
            page.wait_for_selector("text=Passwords don't match", timeout=5000)
            check("mismatched confirmation is caught client-side", True)
            page.fill("#confirm", "brandnewpass1")
            page.screenshot(path=f"{SHOT_DIR}/pr_reset_form.png")
            page.get_by_role("button", name="Reset password").click()
            page.wait_for_selector('button[title="Settings"]', timeout=20000)
            check("completing the reset signs the member straight in", True)
            page.close()

            # --- old password dead, link dead, new password works ------------
            check("old password rejected by the API",
                  requests.post(BASE + "/auth/login",
                                json={"email": "m@local.io", "password": "password123"},
                                timeout=5).status_code == 401)
            dead = browser.new_page(viewport={"width": 1100, "height": 800})
            dead.goto(url, wait_until="networkidle")
            dead.wait_for_selector("text=Reset link not found", timeout=10000)
            check("used link shows the invalid screen", True)
            dead.screenshot(path=f"{SHOT_DIR}/pr_link_dead.png")
            dead.close()

            fresh = browser.new_page(viewport={"width": 1100, "height": 800})
            fresh.goto(BASE + "/", wait_until="networkidle")
            fresh.wait_for_selector("#email", timeout=15000)
            fresh.fill("#email", "m@local.io")
            fresh.fill("#password", "brandnewpass1")
            fresh.get_by_role("button", name="Sign in").click()
            fresh.wait_for_selector('button[title="Settings"]', timeout=20000)
            check("member logs in with the new password", True)
            fresh.close()
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
