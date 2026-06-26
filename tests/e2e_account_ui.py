"""E2E: account self-service through the UI (real browser, no model).

Navigates to the Account page, edits the profile name, changes the password,
creates + revokes an API key (verifying the once-shown token), and adds +
removes a per-user secret (verifying the value is never returned by the API).

Run: .venv/bin/python tests/e2e_account_ui.py
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
PORT = 8921
BASE = f"http://127.0.0.1:{PORT}"

_passed = _failed = 0


def check(name, ok):
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

    root = tempfile.mkdtemp(prefix="acct-ui-")
    iddir = os.path.join(root, "identity")
    os.makedirs(iddir, exist_ok=True)
    from adk_cc.identity.store import JsonFileUserStore
    from adk_cc.identity.provider import EmailPasswordProvider
    store = JsonFileUserStore(os.path.join(iddir, "users.json"))
    EmailPasswordProvider(store, mode="single", global_tenant_id="acme").provision(
        email="alice@acme.io", password="password123", name="Alice", tenant_id="acme", roles=["admin"])

    # A skill that DECLARES a required secret, so the Secrets panel renders a
    # group + "needs setup" badge and the gear icon shows a count.
    skdir = os.path.join(root, "skills", "demo-skill")
    os.makedirs(skdir, exist_ok=True)
    with open(os.path.join(skdir, "SKILL.md"), "w") as f:
        f.write(
            "---\nname: demo-skill\n"
            "description: A demo skill that requires an API token to reach a third-party service.\n"
            "metadata:\n"
            '  x-adk-cc/secrets: \'[{"id":"DEMO_TOKEN","description":"Demo service API token","secret":true}]\'\n'
            "---\nBody.\n"
        )

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1", "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "acme", "ADK_CC_IDENTITY_DIR": iddir,
        "ADK_CC_SKILLS_DIR": os.path.join(root, "skills"),
        "ADK_CC_SERVE_UI": "1", "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub",
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
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector("#email", timeout=10000)
            page.fill("#email", "alice@acme.io")
            page.fill("#password", "password123")
            page.click("button[type=submit]")
            page.wait_for_selector('button[title*="Settings"]', timeout=20000)

            # navigate to the Account page
            page.goto(BASE + "/account", wait_until="networkidle")
            page.wait_for_selector("text=Account", timeout=10000)
            page.wait_for_selector("text=Change password", timeout=10000)
            email_val = page.locator("input[disabled]").first.input_value()
            check("Account page loads (profile + password + api keys sections)",
                  page.get_by_text("API keys").count() > 0
                  and page.get_by_text("Change password").count() > 0
                  and email_val == "alice@acme.io")

            # update name (the Name input is the editable, non-disabled text input
            # in the Profile section; email input is disabled)
            name_input = page.locator("input:not([disabled])").first
            name_input.fill("Alice Updated")
            # scope to the Profile section — the Secrets section also has Save buttons
            page.locator("section").filter(has_text="Profile").get_by_role("button", name="Save").click()
            page.wait_for_selector("text=Saved.", timeout=10000)
            check("profile name save shows confirmation", page.get_by_text("Saved.").count() > 0)
            # persisted on the server
            tok = requests.post(BASE + "/auth/login",
                                json={"email": "alice@acme.io", "password": "password123"}, timeout=5).json()["access_token"]
            me = requests.get(BASE + "/auth/me", headers={"Authorization": f"Bearer {tok}"}, timeout=5).json()
            check("name persisted to the server", me.get("name") == "Alice Updated")

            # change password via UI
            page.fill('input[placeholder="Current password"]', "password123")
            page.fill('input[placeholder="New password (min 8 chars)"]', "newpassword1")
            page.get_by_role("button", name="Update password").click()
            page.wait_for_selector("text=Password changed.", timeout=10000)
            check("password change shows confirmation", page.get_by_text("Password changed.").count() > 0)
            check("new password works on the server",
                  requests.post(BASE + "/auth/login",
                                json={"email": "alice@acme.io", "password": "newpassword1"}, timeout=5).status_code == 200)

            # create an API key → token shown once
            page.fill('input[placeholder="key name (e.g. ci)"]', "laptop")
            page.get_by_role("button", name="Create key").click()
            page.wait_for_selector("text=won't be shown again", timeout=10000)
            # scope to the fresh-token section ("won't be shown again" is unique
            # to it — the Secrets section also has <code> tags and mentions
            # "API keys" in its description)
            shown_token = (page.locator("section").filter(has_text="won't be shown again")
                           .locator("code").first.inner_text().strip())
            check("created API key shows a one-time token (a JWT)",
                  len(shown_token.split(".")) == 3)
            check("the new key is listed",
                  page.locator("li").filter(has_text="laptop").count() > 0)
            # the shown token actually works
            check("shown PAT authorizes a gated API call",
                  requests.get(BASE + "/list-apps", headers={"Authorization": f"Bearer {shown_token}"}, timeout=5).status_code == 200)

            # revoke it → row gone, token rejected
            page.locator("li").filter(has_text="laptop").get_by_role("button", name="Revoke").click()
            time.sleep(0.8)
            check("revoked key removed from the list",
                  page.locator("li").filter(has_text="laptop").count() == 0)
            check("revoked PAT now rejected (401)",
                  requests.get(BASE + "/list-apps", headers={"Authorization": f"Bearer {shown_token}"}, timeout=5).status_code == 401)

            # --- Secrets panel (per-user skills/MCP credentials) ---
            # password was changed to newpassword1 above; use it for server checks.
            tok2 = requests.post(BASE + "/auth/login",
                                 json={"email": "alice@acme.io", "password": "newpassword1"}, timeout=5).json()["access_token"]
            h2 = {"Authorization": f"Bearer {tok2}"}
            check("Custom variables section renders", page.get_by_text("Custom variables").count() > 0)

            # the declaring skill renders as a collapsible card (auto-expanded as
            # it needs setup), surfacing its variable inline
            check("declared skill renders as a card", page.get_by_text("demo-skill").count() > 0)
            check("card shows a needs-setup badge", page.get_by_text("1 needs setup").count() > 0)
            # cards are collapsed by default — expand to reveal the inline variable
            page.locator("button").filter(has_text="demo-skill").first.click()
            page.wait_for_selector("text=DEMO_TOKEN", timeout=8000)
            check("declared env var (DEMO_TOKEN) listed inline", page.get_by_text("DEMO_TOKEN").count() > 0)

            # the gear badge (Settings icon) reflects the missing count on /chat
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector('button[title*="Settings"]', timeout=10000)
            check("Settings gear shows a missing-secrets badge",
                  page.locator('button[title*="need setup"]').count() > 0)
            # opening the Settings modal → the Skills sidebar tab carries the badge
            page.locator('button[title*="Settings"]').click()
            page.wait_for_selector("text=Appearance", timeout=8000)  # modal open (Account tab)
            skills_tab = page.get_by_role("button", name="Skills")
            skills_tab.locator("text=1").wait_for(timeout=5000)  # badge appears after listSecrets
            check("Settings modal Skills tab shows the missing badge",
                  skills_tab.get_by_text("1", exact=True).count() > 0)
            page.keyboard.press("Escape")
            page.goto(BASE + "/account", wait_until="networkidle")
            page.wait_for_selector("text=Custom variables", timeout=10000)

            # add a custom variable via the UI (CUSTOM_KEY + value → Add)
            page.fill('input[placeholder="CUSTOM_KEY"]', "MY_API_KEY")
            page.fill('input[placeholder="value"]', "super-secret-xyz")
            page.locator("section").filter(has_text="Custom variables").get_by_role("button", name="Add").click()
            page.wait_for_selector("text=MY_API_KEY", timeout=10000)
            row = page.locator("li").filter(has_text="MY_API_KEY")
            check("new secret appears with a Set badge",
                  row.count() > 0 and row.get_by_text("Set").count() > 0)

            # server stores it under the user scope (custom key → "other"
            # bucket since no skill/MCP declares it), and NEVER returns the value
            secrets = requests.get(BASE + "/auth/secrets", headers=h2, timeout=5).json()["other"]
            item = next((s for s in secrets if s["key"] == "MY_API_KEY"), None)
            check("secret stored at user scope on the server", item is not None and item["status"] == "user")
            check("secret value is never returned by the API",
                  "super-secret-xyz" not in requests.get(BASE + "/auth/secrets", headers=h2, timeout=5).text)

            # remove it → row gone, server no longer lists it
            row.get_by_title("Remove your value").click()  # the Trash (Remove) button
            time.sleep(0.8)
            check("removed secret disappears from the list",
                  page.locator("li").filter(has_text="MY_API_KEY").count() == 0)
            secrets2 = requests.get(BASE + "/auth/secrets", headers=h2, timeout=5).json()["other"]
            check("secret removed on the server",
                  not any(s["key"] == "MY_API_KEY" for s in secrets2))

            browser.close()
        print(f"\naccount UI e2e: {_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
