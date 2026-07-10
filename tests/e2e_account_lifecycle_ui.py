"""E2E: account danger zone through the UI (real browser).

A member changes their email from the Account page, then deletes their account
via the password-confirmed danger-zone flow: they land back on the login form,
and both old credentials and the account itself are gone server-side.

Model-free — no chat messages are sent, so no LLM calls.
Run: .venv/bin/python tests/e2e_account_lifecycle_ui.py
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
PORT = 8925
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

    root = tempfile.mkdtemp(prefix="ui-al-")
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

        at = requests.post(BASE + "/auth/login",
                           json={"email": "admin@local.io", "password": "adminpass123"},
                           timeout=5).json()["access_token"]
        requests.post(BASE + "/orgs/members",
                      headers={"Authorization": f"Bearer {at}"},
                      json={"email": "m@local.io", "password": "password123",
                            "role": "member"}, timeout=5)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector("#email", timeout=15000)
            page.fill("#email", "m@local.io")
            page.fill("#password", "password123")
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_selector('button[title="Settings"]', timeout=20000)

            # --- change email from the Account page -------------------------
            page.goto(BASE + "/account", wait_until="networkidle")
            page.wait_for_selector("text=Change email", timeout=10000)
            page.locator('input[placeholder^="new address"]').fill("m2@local.io")
            page.locator('section', has_text="Change email").locator('input[type="password"]').fill("password123")
            page.get_by_role("button", name="Update email").click()
            page.wait_for_selector("text=Email changed.", timeout=10000)
            check("email changed via the UI", True)
            check("old email dead / new email lives (API)",
                  requests.post(BASE + "/auth/login",
                                json={"email": "m@local.io", "password": "password123"},
                                timeout=5).status_code == 401
                  and requests.post(BASE + "/auth/login",
                                    json={"email": "m2@local.io", "password": "password123"},
                                    timeout=5).ok)

            # --- delete via the danger zone ---------------------------------
            page.wait_for_selector("text=Danger zone", timeout=10000)
            page.get_by_role("button", name="Delete account").click()
            page.wait_for_selector('input[placeholder="Confirm with your password"]', timeout=5000)
            page.screenshot(path=f"{SHOT_DIR}/al_danger.png")
            page.fill('input[placeholder="Confirm with your password"]', "password123")
            page.get_by_role("button", name="Delete permanently").click()
            page.wait_for_selector("#email", timeout=20000)
            check("deletion bounces to the login form", True)
            check("account is gone server-side",
                  requests.post(BASE + "/auth/login",
                                json={"email": "m2@local.io", "password": "password123"},
                                timeout=5).status_code == 401)
            page.close()
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
