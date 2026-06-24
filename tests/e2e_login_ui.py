"""E2E: the full login journey driven entirely through the UI (real browser).

Multi mode: email+password form shown → sign-up enters the app → session
persists across reload → sign-out returns to login → log back IN with the same
account → a wrong password surfaces an error and stays on the form.
Single mode: no sign-up toggle (registration off); the bootstrapped admin logs
in through the form.

Model-free — no chat messages are sent, so no LLM calls. Safe to run unthrottled.
Run: .venv/bin/python tests/e2e_login_ui.py
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
SHOT_DIR = "/tmp"

_passed = _failed = 0


def check(name: str, ok: bool) -> None:
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def _server(port: int, extra_env: dict):
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_SERVE_UI": "1",
        "ADK_CC_SKIP_DOTENV": "1",
        "ADK_CC_API_KEY": "stub",
    })
    env.update(extra_env)
    env.pop("ADK_CC_ALLOW_NO_AUTH", None)
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            if requests.get(base + "/auth/config", timeout=2).ok:
                return proc, base
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("server did not start")


def _wait_app(page) -> None:
    page.wait_for_selector('button[title="Settings"]', timeout=20000)


def _wait_login(page) -> None:
    page.wait_for_selector("#email", timeout=15000)


def _sign_out(page) -> None:
    page.click('button[title="Settings"]')
    page.get_by_text("Sign out").first.click()  # clearToken() + reload
    _wait_login(page)


def run_multi(pw) -> None:
    root = tempfile.mkdtemp(prefix="ui-multi-")
    proc, base = _server(8913, {"ADK_CC_TENANCY_MODE": "multi", "ADK_CC_IDENTITY_DIR": root})
    EMAIL, PASSWORD = "founder@startup.io", "password123"
    try:
        print("multi mode — full UI journey:")
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        page.goto(base + "/", wait_until="networkidle")

        # 1. password form, with a sign-up toggle
        _wait_login(page)
        check("email+password form (not token paste)",
              page.locator("#email").count() == 1 and page.locator("#token").count() == 0)
        check("sign-up toggle present", page.get_by_text("Create one").count() > 0)
        page.screenshot(path=f"{SHOT_DIR}/ui_login.png")

        # 2. sign up → land in the app
        page.get_by_text("Create one").first.click()
        page.wait_for_selector("#org", timeout=5000)
        page.fill("#email", EMAIL)
        page.fill("#password", PASSWORD)
        page.fill("#org", "Startup Co")
        page.click("button[type=submit]")
        _wait_app(page)
        check("sign-up authenticates and enters the app",
              page.locator("#email").count() == 0)

        # 3. reload → session persists (token re-verified, no re-login)
        page.reload(wait_until="networkidle")
        _wait_app(page)
        check("session persists across reload", page.locator("#email").count() == 0)

        # 4. sign out → back to the login form
        _sign_out(page)
        check("sign-out returns to the login form", page.locator("#email").count() == 1)

        # 5. log IN with the same account (the login path, not signup)
        page.fill("#email", EMAIL)
        page.fill("#password", PASSWORD)
        page.click("button[type=submit]")
        _wait_app(page)
        check("log back in with email+password", page.locator("#email").count() == 0)

        # 6. sign out, then a wrong password surfaces an error and stays on form
        _sign_out(page)
        page.fill("#email", EMAIL)
        page.fill("#password", "WRONG-password")
        page.click("button[type=submit]")
        page.wait_for_selector(".text-destructive", timeout=10000)
        check("wrong password shows an error and stays on the form",
              page.locator(".text-destructive").count() > 0 and page.locator("#email").count() == 1)
        page.screenshot(path=f"{SHOT_DIR}/ui_login_error.png")
        browser.close()
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


def run_single(pw) -> None:
    root = tempfile.mkdtemp(prefix="ui-single-")
    proc, base = _server(8914, {
        "ADK_CC_TENANCY_MODE": "single", "ADK_CC_IDENTITY_DIR": root,
        "ADK_CC_BOOTSTRAP_ADMIN_EMAIL": "admin@corp.io",
        "ADK_CC_BOOTSTRAP_ADMIN_PASSWORD": "password123",
    })
    try:
        print("single mode — no signup, admin logs in via UI:")
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        page.goto(base + "/", wait_until="networkidle")
        _wait_login(page)
        check("no sign-up toggle (registration disabled)",
              page.get_by_text("Create one").count() == 0)
        page.fill("#email", "admin@corp.io")
        page.fill("#password", "password123")
        page.click("button[type=submit]")
        _wait_app(page)
        check("bootstrapped admin logs in through the form",
              page.locator("#email").count() == 0)
        browser.close()
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    if not os.path.isfile(os.path.join(REPO, "web", "dist", "index.html")):
        print("SKIP: web/dist not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0
    with sync_playwright() as pw:
        run_multi(pw)
        run_single(pw)
    print(f"\nlogin UI e2e: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
