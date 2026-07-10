"""E2E: brute-force lockout as the user sees it (real browser).

Server runs with a 3-failure / 2-second lockout. Three wrong passwords in the
login form → the form shows plain 'invalid' errors; the fourth attempt (even
with the CORRECT password) surfaces the "too many failed attempts" message in
the form; after the window ages out, the correct password signs in.

Model-free — no chat messages are sent, so no LLM calls.
Run: .venv/bin/python tests/e2e_lockout_ui.py
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
PORT = 8926
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

    root = tempfile.mkdtemp(prefix="ui-lk-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1",
        "ADK_CC_SERVE_UI": "1",
        "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_IDENTITY_DIR": root,
        "ADK_CC_BOOTSTRAP_ADMIN_EMAIL": "admin@local.io",
        "ADK_CC_BOOTSTRAP_ADMIN_PASSWORD": "adminpass123",
        "ADK_CC_AUTH_LOCKOUT_THRESHOLD": "3",
        "ADK_CC_AUTH_LOCKOUT_S": "2",
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

            def attempt(pw_: str) -> None:
                page.fill("#email", "admin@local.io")
                page.fill("#password", pw_)
                page.get_by_role("button", name="Sign in").click()

            for i in range(3):
                attempt(f"wrong-{i}")
                page.wait_for_selector("text=Invalid email or password", timeout=10000)
            check("wrong attempts show the plain invalid-credentials error", True)

            attempt("adminpass123")  # correct, but the pair is now locked
            page.wait_for_selector("text=too many failed attempts", timeout=10000)
            check("locked: the form shows the too-many-attempts message", True)
            page.screenshot(path=f"{SHOT_DIR}/lk_locked.png")

            time.sleep(2.2)  # lockout ages out
            attempt("adminpass123")
            page.wait_for_selector('button[title="Settings"]', timeout=20000)
            check("after the lockout window the correct password signs in", True)
            page.close()
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
