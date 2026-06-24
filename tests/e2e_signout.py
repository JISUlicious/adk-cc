"""E2E: signing out actually sticks — even on a no-auth dev server.

Regression for the bug where sign-out flashed the login screen then bounced
straight back to chat (the no-auth auto-login re-authenticated). After signing
out we must LAND on the login form and STAY there.

Model-free. Run: .venv/bin/python tests/e2e_signout.py
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
PORT = 8918
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

    root = tempfile.mkdtemp(prefix="signout-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",  # the mode that triggered the bug
        "ADK_CC_SERVE_UI": "1",
        "ADK_CC_WORKSPACE_ROOT": root,
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub",
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1100, "height": 800})

            # no-auth dev mode auto-signs-in → straight to chat
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector('button[title="Settings"]', timeout=20000)
            check("no-auth dev mode auto-signs in to chat",
                  page.locator('button[title="Settings"]').count() == 1)

            # sign out via the Settings dialog
            page.click('button[title="Settings"]')
            page.get_by_text("Sign out").first.click()

            # must land on the signed-out screen (no-auth → one-click Continue,
            # NOT a dead-end token form you can't fill)…
            page.wait_for_selector("text=Signed out", timeout=10000)
            cont = page.get_by_role("button", name="Continue")
            check("sign-out lands on the signed-out screen with Continue", cont.count() == 1)

            # …and STAY there — it must NOT bounce back into chat
            time.sleep(2.0)
            check("does NOT bounce back to chat after sign-out",
                  page.locator('button[title="Settings"]').count() == 0
                  and page.get_by_role("button", name="Continue").count() == 1)

            # a hard reload keeps us signed out (marker persists in the tab)
            page.reload(wait_until="networkidle")
            page.wait_for_selector("text=Signed out", timeout=10000)
            check("stays signed out across reload",
                  page.get_by_role("button", name="Continue").count() == 1)

            # Continue → back into the app (this is the "log back in" path)
            page.get_by_role("button", name="Continue").click()
            page.wait_for_selector('button[title="Settings"]', timeout=20000)
            check("Continue re-enters the app", page.locator('button[title="Settings"]').count() == 1)

            browser.close()
        print(f"\nsign-out e2e: {_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
