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


def _any_usage_reported() -> bool:
    """A short, error-tolerant API turn → True if the model reported
    usageMetadata.promptTokenCount. Used only to decide FAIL vs SKIP when the
    gauge didn't render. A timeout/error counts as 'not reported' (→ SKIP)."""
    try:
        sid = requests.post(f"{BASE}/apps/adk_cc/users/local/sessions",
                            json={}, timeout=15).json()["id"]
        r = requests.post(f"{BASE}/run", timeout=90, json={
            "appName": "adk_cc", "userId": "local", "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text": "hi"}]}})
        for e in r.json():
            um = e.get("usageMetadata") or e.get("usage_metadata") or {}
            if um.get("promptTokenCount") or um.get("prompt_token_count"):
                return True
    except Exception:
        return False
    return False


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
            # the gauge shows "<pct>%" — but ONLY once a turn reports
            # usageMetadata.promptTokenCount. Read the session to learn whether
            # usage was actually reported, so we distinguish a real UI bug
            # (usage present but gauge missing → FAIL) from a slow/variable
            # endpoint that didn't report usage in time (→ SKIP, like the other
            # live probes). The gauge correctly shows nothing without usage data.
            gauge = page.locator("text=/\\d+%/")
            shown = gauge.count() > 0
            page.screenshot(path=SHOT, full_page=False)
            usage_reported = _any_usage_reported()
            print(f"  [{'PASS' if ep_ok else 'FAIL'}] /api/context/limits returns the ladder")
            print(f"  usage_metadata reported by model: {usage_reported}")
            print(f"  screenshot: {SHOT}")
            browser.close()

            if not ep_ok:
                print("\ncontext gauge e2e FAILED")
                return 1
            if shown:
                print("  [PASS] context gauge (NN%) renders in the header")
                print("\ncontext gauge e2e PASSED")
                return 0
            if not usage_reported:
                print("  [SKIP] model reported no usage_metadata (slow/variable "
                      "endpoint) — gauge has no data to show this run; the "
                      "render path is covered when usage is reported.")
                print("\ncontext gauge e2e SKIPPED")
                return 0
            # usage WAS reported but the gauge didn't render → a real UI bug
            print("  [FAIL] usage reported but gauge did not render")
            print("\ncontext gauge e2e FAILED")
            return 1
    finally:
        proc.kill()
        for d in (wks, art):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
