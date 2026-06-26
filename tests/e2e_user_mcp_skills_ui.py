"""Real-browser e2e: per-user MCP servers & skills via the Account UI (Phase 5).

alice adds a personal MCP server through the form and uploads a personal skill
zip through the file input; verifies both render, the Secrets grouping picks up
their env vars, and the Settings gear badge reflects the new required values.
Also captures a screenshot. Model-free.

Run: .venv/bin/python tests/e2e_user_mcp_skills_ui.py
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8933
BASE = f"http://127.0.0.1:{PORT}"
OUT = os.environ.get("ADK_CC_SHOT_DIR", tempfile.gettempdir())
_passed = _failed = 0


def check(name, ok):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    _passed += 1 if ok else 0
    _failed += 0 if ok else 1


def main() -> int:
    if not os.path.isfile(os.path.join(REPO, "web", "dist", "index.html")):
        print("SKIP: web/dist not built."); return 0
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    root = tempfile.mkdtemp(prefix="user-ms-ui-")
    iddir = os.path.join(root, "identity"); os.makedirs(iddir)
    from adk_cc.identity.store import JsonFileUserStore
    from adk_cc.identity.provider import EmailPasswordProvider
    store = JsonFileUserStore(os.path.join(iddir, "users.json"))
    EmailPasswordProvider(store, mode="single", global_tenant_id="acme").provision(
        email="alice@acme.io", password="password123", name="Alice", tenant_id="acme", roles=["admin"])

    # a skill zip on disk for the file input
    zip_path = os.path.join(root, "myskill.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SKILL.md",
                    "---\nname: myskill\ndescription: A personal skill needing a token.\n"
                    "metadata:\n  x-adk-cc/secrets: '[{\"id\":\"MYSKILL_TOKEN\"}]'\n---\n\nBody.\n")

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1", "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "acme", "ADK_CC_IDENTITY_DIR": iddir,
        "ADK_CC_TENANT_REGISTRY_DIR": os.path.join(root, "registry"),
        "ADK_CC_TENANT_SKILLS_DIR": os.path.join(root, "skills"),
        "ADK_CC_CREDENTIAL_PROVIDER": "memory",
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
            page = browser.new_page(viewport={"width": 1120, "height": 1400})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector("#email", timeout=10000)
            page.fill("#email", "alice@acme.io")
            page.fill("#password", "password123")
            page.click("button[type=submit]")
            page.wait_for_selector('button[title*="Settings"]', timeout=20000)
            page.goto(BASE + "/account", wait_until="networkidle")
            page.wait_for_selector("text=MCP servers", timeout=10000)
            check("MCP servers section renders", page.get_by_text("MCP servers").count() > 0)
            check("Skills section renders", page.get_by_text("Upload a personal skill").count() > 0)

            # add a personal MCP server via the form
            mcp = page.locator("section").filter(has_text="MCP servers")
            mcp.locator('input[placeholder="server name"]').fill("mybox")
            mcp.locator('input[placeholder="https://… or command"]').fill("https://example/mcp")
            mcp.locator('input[placeholder="credential_key (optional)"]').fill("MYBOX_TOKEN")
            mcp.get_by_role("button", name="Add server").click()
            page.wait_for_selector("text=mybox", timeout=10000)
            check("personal MCP server appears as a Personal card",
                  mcp.locator("button").filter(has_text="mybox").get_by_text("Personal").count() > 0)

            # upload a personal skill via the file input
            skills = page.locator("section").filter(has_text="Upload a personal skill")
            skills.locator('input[type="file"]').set_input_files(zip_path)
            skills.get_by_role("button", name="Upload").click()
            page.wait_for_selector("text=myskill", timeout=10000)
            check("personal skill appears as a card",
                  skills.locator("button").filter(has_text="myskill").count() > 0)

            # after reload, each item's required variable shows INLINE in its card
            # (cards auto-expand because the values aren't set yet)
            page.reload(wait_until="networkidle")
            page.wait_for_selector("text=MCP servers", timeout=10000)
            check("MCP server card present", page.get_by_text("mybox").count() > 0)
            check("skill card present", page.get_by_text("myskill").count() > 0)
            check("MYBOX_TOKEN shown inline under its server", page.get_by_text("MYBOX_TOKEN").count() > 0)
            check("MYSKILL_TOKEN shown inline under its skill", page.get_by_text("MYSKILL_TOKEN").count() > 0)

            # gear badge reflects the 2 new required secrets
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector('button[title*="Settings"]', timeout=10000)
            check("Settings gear shows a missing-secrets badge",
                  page.locator('button[title*="need setup"]').count() > 0)

            page.goto(BASE + "/account", wait_until="networkidle")
            page.wait_for_selector("text=MCP servers", timeout=10000)
            page.wait_for_timeout(400)
            page.screenshot(path=os.path.join(OUT, "ui_user_mcp_skills.png"), full_page=True)
            browser.close()

        print(f"\nuser MCP/skills UI e2e: {_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
