"""E2E: the access-request journey driven entirely through the UI (real browser).

Single mode + bootstrap admin: login form offers "Request access" (no sign-up) →
Jane files a request with a note → confirmation screen → her login shows the
awaiting-approval message → admin opens Team, sees the queue, approves → Jane
logs in and lands in the app. Bob's request is rejected in the UI and vanishes.

Model-free — no chat messages are sent, so no LLM calls. Safe to run unthrottled.
Run: .venv/bin/python tests/e2e_access_requests_ui.py
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
PORT = 8917

_passed = _failed = 0


def check(name: str, ok: bool) -> None:
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def _server():
    root = tempfile.mkdtemp(prefix="ui-req-")
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
    base = f"http://127.0.0.1:{PORT}"
    for _ in range(80):
        try:
            if requests.get(base + "/auth/config", timeout=2).ok:
                return proc, base
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("server did not start")


def _wait_login(page) -> None:
    page.wait_for_selector("#email", timeout=15000)


def main() -> int:
    from playwright.sync_api import sync_playwright

    proc, base = _server()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)

            # --- Jane: request access through the form ---------------------
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            page.goto(base + "/", wait_until="networkidle")
            _wait_login(page)
            check("login form offers Request access (and no sign-up)",
                  page.get_by_text("Request access").count() > 0
                  and page.get_by_text("Create one").count() == 0)

            page.get_by_text("Request access").first.click()
            page.wait_for_selector("#note", timeout=5000)
            check("request form has name + note fields",
                  page.locator("#name").count() == 1 and page.locator("#note").count() == 1)
            page.fill("#email", "jane@example.com")
            page.fill("#password", "janepass123")
            page.fill("#name", "Jane")
            page.fill("#note", "QA team")
            page.screenshot(path=f"{SHOT_DIR}/req_form.png")
            page.get_by_role("button", name="Request access").click()
            page.wait_for_selector("text=Request submitted", timeout=10000)
            check("confirmation screen shown", page.get_by_text("Request submitted").count() == 1)
            page.screenshot(path=f"{SHOT_DIR}/req_submitted.png")

            # back to sign-in → pending login is blocked with the message
            page.get_by_text("Back to sign in").click()
            _wait_login(page)
            page.fill("#email", "jane@example.com")
            page.fill("#password", "janepass123")
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_selector("text=awaiting admin approval", timeout=10000)
            check("pending login shows the awaiting-approval message",
                  page.get_by_text("awaiting admin approval").count() == 1)
            page.screenshot(path=f"{SHOT_DIR}/req_pending_login.png")
            page.close()

            # --- Admin: review the queue in Team ---------------------------
            admin = browser.new_page(viewport={"width": 1100, "height": 800})
            admin.goto(base + "/", wait_until="networkidle")
            _wait_login(admin)
            admin.fill("#email", "admin@local.io")
            admin.fill("#password", "adminpass123")
            admin.get_by_role("button", name="Sign in").click()
            admin.wait_for_selector('button[title="Settings"]', timeout=20000)

            admin.goto(base + "/org", wait_until="networkidle")
            admin.wait_for_selector("text=Access requests (1)", timeout=10000)
            check("admin sees the request with email, name, note",
                  admin.get_by_text("jane@example.com").count() > 0
                  and admin.get_by_text("QA team").count() > 0)
            admin.screenshot(path=f"{SHOT_DIR}/req_queue.png")

            admin.get_by_role("button", name="Approve").click()
            admin.wait_for_selector("text=Access requests", state="detached", timeout=10000)
            check("queue empties and jane appears under Members",
                  admin.get_by_text("jane@example.com").count() > 0)
            admin.screenshot(path=f"{SHOT_DIR}/req_approved.png")

            # --- Bob: rejected in the UI ------------------------------------
            requests.post(base + "/auth/request-access",
                          json={"email": "bob@example.com", "password": "bobpass1234"},
                          timeout=5)
            admin.reload(wait_until="networkidle")
            admin.wait_for_selector("text=Access requests (1)", timeout=10000)
            admin.get_by_role("button", name="Reject").click()
            admin.wait_for_selector("text=Access requests", state="detached", timeout=10000)
            check("rejected request vanishes from the queue",
                  admin.get_by_text("bob@example.com").count() == 0)
            admin.close()

            # --- Jane: approved → logs in and lands in the app -------------
            jane = browser.new_page(viewport={"width": 1100, "height": 800})
            jane.goto(base + "/", wait_until="networkidle")
            _wait_login(jane)
            jane.fill("#email", "jane@example.com")
            jane.fill("#password", "janepass123")
            jane.get_by_role("button", name="Sign in").click()
            jane.wait_for_selector('button[title="Settings"]', timeout=20000)
            check("approved jane signs in and lands in the app", True)
            jane.screenshot(path=f"{SHOT_DIR}/req_jane_in.png")
            jane.close()
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
