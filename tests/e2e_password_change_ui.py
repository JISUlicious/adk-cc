"""E2E: changing your password in the UI must NOT log you out (real browser).

Server runs a 3s access TTL. A user changes their password on the Account page;
the server revokes every refresh token (including the one the SPA held) and
returns a fresh pair. The SPA must adopt that pair — so after the old access
token expires, the app keeps working (silent refresh with the NEW token)
instead of bouncing to login with the now-revoked old one.

Model-free. Run: .venv/bin/python tests/e2e_password_change_ui.py
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
PORT = 8931
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

    root = tempfile.mkdtemp(prefix="ui-pc-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_SERVE_UI": "1",
        "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_IDENTITY_DIR": root,
        "ADK_CC_BOOTSTRAP_ADMIN_EMAIL": "admin@local.io",
        "ADK_CC_BOOTSTRAP_ADMIN_PASSWORD": "adminpass123",
        "ADK_CC_AUTH_TOKEN_TTL_S": "3",
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

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector("#email", timeout=15000)
            page.fill("#email", "admin@local.io")
            page.fill("#password", "adminpass123")
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_selector('button[title="Settings"]', timeout=20000)
            refresh0 = page.evaluate("localStorage.getItem('adk_cc.refresh')")

            page.goto(BASE + "/account", wait_until="networkidle")
            page.wait_for_selector("text=Change password", timeout=10000)
            sec = page.locator("section", has_text="Change password")
            sec.locator('input[autocomplete="current-password"]').fill("adminpass123")
            sec.locator('input[autocomplete="new-password"]').fill("brandnew12345")
            page.get_by_role("button", name="Update password").click()
            page.wait_for_selector("text=Password changed.", timeout=10000)
            check("password changed via the UI", True)

            refresh1 = page.evaluate("localStorage.getItem('adk_cc.refresh')")
            check("SPA adopted a fresh refresh token", bool(refresh1) and refresh1 != refresh0)
            check("the pre-change refresh token was revoked server-side",
                  requests.post(BASE + "/auth/refresh",
                                json={"refresh_token": refresh0}, timeout=5).status_code == 401)

            time.sleep(4)  # old 3s access token now expired
            # keep using the app — must silently refresh with the NEW token, not bounce
            page.goto(BASE + "/account", wait_until="networkidle")
            # The Account page renders only for an authenticated session; the login
            # form shows #email. Reaching Danger zone with no #email = no bounce.
            page.wait_for_selector("text=Danger zone", timeout=10000)
            check("app still works past expiry (no bounce to login)",
                  page.locator("#email").count() == 0
                  and page.get_by_role("button", name="Delete account").count() > 0)
            page.screenshot(path=f"{SHOT_DIR}/pc_after.png")
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
