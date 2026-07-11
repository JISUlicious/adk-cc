"""E2E: two tabs sharing one session must not log each other out (real browser).

The #4 fix. Two pages in ONE browser context share localStorage. With a 3s
access TTL, both tabs' access tokens expire; when both then make an authed
request at ~the same time they each try to rotate the ONE shared refresh token.
Without cross-tab coordination the loser's stale token trips reuse-detection and
revokes the winner's fresh token, dumping BOTH tabs to login. With the Web-Locks
single-flight, one tab refreshes and the other adopts the result — both survive.

Model-free. Run: .venv/bin/python tests/e2e_two_tab_refresh_ui.py
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
PORT = 8932
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

    root = tempfile.mkdtemp(prefix="ui-2tab-")
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
            ctx = browser.new_context(viewport={"width": 1000, "height": 760})

            # tab 1 signs in → both tabs share this context's localStorage
            tab1 = ctx.new_page()
            tab1.goto(BASE + "/", wait_until="networkidle")
            tab1.wait_for_selector("#email", timeout=15000)
            tab1.fill("#email", "admin@local.io")
            tab1.fill("#password", "adminpass123")
            tab1.get_by_role("button", name="Sign in").click()
            tab1.wait_for_selector('button[title="Settings"]', timeout=20000)

            # tab 2 boots with the shared token (no login form)
            tab2 = ctx.new_page()
            tab2.goto(BASE + "/", wait_until="networkidle")
            tab2.wait_for_selector('button[title="Settings"]', timeout=20000)
            check("second tab adopted the shared session", True)

            time.sleep(4)  # both 3s access tokens now expired

            # Reload BOTH near-simultaneously — each boots, 401s, and races to
            # rotate the one shared refresh token. wait_until="commit" returns
            # without blocking for load, so the two boots overlap.
            tab1.goto(BASE + "/", wait_until="commit")
            tab2.goto(BASE + "/", wait_until="commit")

            # Both must land in the app (Settings present), neither on #email.
            tab1.wait_for_selector('button[title="Settings"]', timeout=20000)
            tab2.wait_for_selector('button[title="Settings"]', timeout=20000)
            check("tab 1 still logged in after concurrent refresh",
                  tab1.locator("#email").count() == 0)
            check("tab 2 still logged in after concurrent refresh",
                  tab2.locator("#email").count() == 0)
            tab1.screenshot(path=f"{SHOT_DIR}/twotab_1.png")

            # The shared session is healthy: the stored refresh token still works.
            rt = tab1.evaluate("localStorage.getItem('adk_cc.refresh')")
            check("the shared refresh token is still valid server-side",
                  requests.post(BASE + "/auth/refresh",
                                json={"refresh_token": rt}, timeout=5).status_code == 200)
            ctx.close()
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
