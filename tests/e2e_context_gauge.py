"""E2E (compaction-indicator P2): the context-fullness gauge renders in the chat
header after a real turn reports usage. Backend /api/context/limits + the
ContextGauge fed by event usageMetadata. Live model; skips without one.

Run: .venv/bin/python tests/e2e_context_gauge.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import adk_cc  # noqa: F401

os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "30")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8789
BASE = f"http://127.0.0.1:{PORT}"
SHOT = "/tmp/context_gauge.png"


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY.")
        return 0
    if not os.path.isfile(os.path.join(REPO, "web", "dist", "index.html")):
        print("SKIP: web/dist not built.")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable.")
        return 0

    wks = tempfile.mkdtemp(prefix="gauge-w-")
    art = tempfile.mkdtemp(prefix="gauge-a-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_SERVE_UI": "1",
        "ADK_CC_WORKSPACE_ROOT": wks, "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{art}",
        "ADK_CC_MAX_CONTEXT_TOKENS": "200000",
        "ADK_CC_TOOL_TITLES": "0",
    })
    for k in ("ADK_CC_COMPACTION_TOKEN_THRESHOLD", "ADK_CC_COMPACTION_EVENT_RETENTION"):
        env.pop(k, None)  # no compaction needed for the gauge
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    try:
        for _ in range(60):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.5)
        # sanity: endpoint returns limits
        lim = requests.get(BASE + "/api/context/limits", timeout=5).json()
        print(f"  /api/context/limits -> {json.dumps(lim)}")
        ep_ok = lim.get("effective") == 200000

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            page.goto(BASE, wait_until="networkidle")
            page.wait_for_selector("select", timeout=15000)
            page.click('button[title="New session"]', timeout=15000)
            page.wait_for_selector("textarea", timeout=15000)
            ta = page.locator("textarea")
            ta.click(); ta.fill("Say a few words about CPU caches."); ta.press("Enter")
            try:
                page.wait_for_selector("text=agent is working", timeout=8000)
            except Exception:
                pass
            try:
                page.wait_for_selector("text=agent is working", state="hidden", timeout=120000)
            except Exception:
                pass
            time.sleep(3)
            # the gauge shows "<pct>%" once usageMetadata has been reported
            body = page.content()
            gauge = page.locator("text=/\\d+%/")
            shown = gauge.count() > 0
            page.screenshot(path=SHOT, full_page=False)
            print(f"  [{'PASS' if ep_ok else 'FAIL'}] /api/context/limits returns the ladder")
            print(f"  [{'PASS' if shown else 'WARN'}] context gauge (NN%) renders in the header")
            print(f"  screenshot: {SHOT}")
            ok = ep_ok and shown
            browser.close()
        print("\ncontext gauge e2e " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        proc.kill()
        for d in (wks, art):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
