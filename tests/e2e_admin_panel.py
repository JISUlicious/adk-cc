"""End-to-end browser test of the admin panel (Playwright).

Boots a real uvicorn server (UI + admin enabled) and drives the /admin page
through Chromium: adds an MCP server, uploads a skill, adds + activates a
model endpoint — asserting each landed via the backend API — and confirms a
non-admin token is blocked.

Run (server managed by the webapp-testing helper or manually):
    ADK_CC_ADMIN_PANEL=1 ADK_CC_SERVE_UI=1 \
    ADK_CC_AUTH_TOKENS="admintok=alice:local:admin,usertok=bob:local" \
    ADK_CC_UI_DIST=$(pwd)/web/dist ADK_CC_AGENTS_DIR=$(pwd)/agents \
    ADK_CC_API_KEY=stub ADK_CC_SKIP_DOTENV=1 \
    .venv/bin/uvicorn adk_cc.service.server:make_app --factory --port 8099 &
    .venv/bin/python tests/e2e_admin_panel.py 8099

Requires `web/dist` (run `npm --prefix web run build` first).
"""

from __future__ import annotations

import io
import sys
import zipfile

import requests
from playwright.sync_api import sync_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
BASE = f"http://127.0.0.1:{PORT}"
ADMIN = "admintok"   # alice:local:admin
USER = "usertok"     # bob:local (no admin role)


def _seed_token(page, token):
    page.goto(BASE + "/")
    page.evaluate(
        "(t) => { localStorage.setItem('adk_cc.token', t); localStorage.setItem('adk_cc.user','u'); }",
        token,
    )


def _api(method, path, token=ADMIN, **kw):
    return requests.request(
        method, BASE + path,
        headers={"Authorization": f"Bearer {token}"}, timeout=10, **kw,
    )


def test_non_admin_blocked_by_api():
    # The page shell loads for anyone, but the gated API rejects a non-admin.
    r = _api("GET", "/tenants/local/mcp-servers", token=USER)
    assert r.status_code == 403, f"expected 403, got {r.status_code}"
    print("OK non-admin API blocked (403)")


def test_add_mcp_via_ui(page):
    page.goto(BASE + "/admin/mcp")
    page.wait_for_load_state("networkidle")
    page.click("text=Add")
    page.fill("input[placeholder='github']", "e2e-mcp")
    page.fill("input[placeholder*='api.github.com']", "https://e2e.example/mcp")
    # transport select → http
    page.select_option("select", "http")
    page.click("text=Save")
    page.wait_for_timeout(800)
    # assert via API
    r = _api("GET", "/tenants/local/mcp-servers")
    names = [s["server_name"] for s in r.json()["servers"]]
    assert "e2e-mcp" in names, names
    print("OK MCP server added via UI")


def test_add_and_activate_model_via_ui(page):
    page.goto(BASE + "/admin/models")
    page.wait_for_load_state("networkidle")
    page.click("text=Add")
    page.fill("input[placeholder='claude']", "e2e-model")
    page.fill("input[placeholder*='anthropic']", "anthropic/claude-x")
    page.fill("input[placeholder*='host:port']", "https://e2e.example/v1")
    page.click("text=Save")
    page.wait_for_timeout(800)
    # activate it: click the activate circle on its row
    # (the row shows the name; click the radio button next to it)
    page.click("button[aria-label='Activate e2e-model']")
    page.wait_for_timeout(800)
    r = _api("GET", "/admin/model-endpoints").json()
    assert r["active"] == "e2e-model", r
    print("OK model endpoint added + activated via UI (live switch)")


def test_upload_skill_via_api_then_listed_in_ui(page):
    # Upload through the API (file-picker automation is brittle), then assert
    # the UI lists it.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "---\nname: e2e-skill\ndescription: d\n---\nbody")
    r = requests.put(
        BASE + "/tenants/local/skills/e2e-skill",
        headers={"Authorization": f"Bearer {ADMIN}", "Content-Type": "application/zip"},
        data=buf.getvalue(), timeout=10,
    )
    assert r.status_code == 200, r.text
    page.goto(BASE + "/admin/skills")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)
    assert "e2e-skill" in page.content()
    print("OK skill uploaded + listed in UI")


def main():
    test_non_admin_blocked_by_api()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        _seed_token(page, ADMIN)
        test_add_mcp_via_ui(page)
        test_add_and_activate_model_via_ui(page)
        test_upload_skill_via_api_then_listed_in_ui(page)
        browser.close()
    print("\nadmin-panel e2e passed")


if __name__ == "__main__":
    main()
