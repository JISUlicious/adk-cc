"""E2E: silent refresh + real logout through the UI (real browser).

Server runs with a 3-second access TTL. Login via the form → wait until the
access token is genuinely dead → keep using the app (Settings → Account fetches
/auth/me): the SPA must silently refresh instead of bouncing to login, and the
stored refresh token must have ROTATED. Sign out → back at the login form AND
the pre-signout refresh token is revoked server-side (revokeSession fired).

Model-free — no chat messages are sent, so no LLM calls.
Run: .venv/bin/python tests/e2e_refresh_ui.py
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
PORT = 8919
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

    root = tempfile.mkdtemp(prefix="ui-rt-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_SERVE_UI": "1",
        "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_IDENTITY_DIR": root,
        "ADK_CC_BOOTSTRAP_ADMIN_EMAIL": "admin@local.io",
        "ADK_CC_BOOTSTRAP_ADMIN_PASSWORD": "adminpass123",
        "ADK_CC_AUTH_TOKEN_TTL_S": "3",  # access dies fast — forces silent refresh
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
            check("login stored a refresh token", bool(refresh0))

            time.sleep(4)  # the 3s access token is now genuinely expired

            # Use the app: Settings → Account fires authed calls (/auth/me …).
            page.click('button[title="Settings"]')
            page.wait_for_selector("text=Account", timeout=10000)
            page.wait_for_selector("text=admin@local.io", timeout=10000)
            check("app keeps working past access expiry (silent refresh, no bounce)",
                  page.locator("#email").count() == 0
                  and page.get_by_text("admin@local.io").count() > 0)
            page.screenshot(path=f"{SHOT_DIR}/rt_after_expiry.png")

            refresh1 = page.evaluate("localStorage.getItem('adk_cc.refresh')")
            check("refresh token rotated in storage", bool(refresh1) and refresh1 != refresh0)

            # Sign out → login form, tokens cleared, refresh revoked server-side.
            page.get_by_text("Sign out").first.click()
            page.wait_for_selector("#email", timeout=15000)
            check("sign-out returns to the login form", True)
            check("sign-out cleared stored tokens",
                  not page.evaluate("localStorage.getItem('adk_cc.refresh')")
                  and not page.evaluate("localStorage.getItem('adk_cc.token')"))
            r = requests.post(BASE + "/auth/refresh",
                              json={"refresh_token": refresh1}, timeout=5)
            check("pre-signout refresh token is revoked server-side (real logout)",
                  r.status_code == 401)

            # and logging back in still works
            page.fill("#email", "admin@local.io")
            page.fill("#password", "adminpass123")
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_selector('button[title="Settings"]', timeout=20000)
            check("re-login works after logout", True)
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
