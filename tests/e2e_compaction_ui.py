"""Live UI e2e: the CompactionDivider actually renders in the chat thread.

Boots a UI-serving server with a tiny compaction threshold, drives the real
React app in a headless browser (Playwright) through several turns until ADK
compacts, and asserts the "Context compacted" divider appears — then expands it
and checks the summary text shows. Complements e2e_compaction_signal.py (which
proves the data path); this proves the render.

Live model required; skips without one. Run:
    .venv/bin/python tests/e2e_compaction_ui.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

import adk_cc  # noqa: F401 — bootstraps .env (model creds)

os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "30")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8779
BASE = f"http://127.0.0.1:{PORT}"
SHOT = "/tmp/compaction_ui.png"
FILLER = (
    "Please keep this CPU microarchitecture background in mind for the rest of "
    "our conversation. Modern high-performance cores use deep pipelines, "
    "out-of-order execution with large reorder buffers, TAGE branch predictors, "
    "multi-level cache hierarchies, simultaneous multithreading, wide decode, "
    "and aggressive prefetching. " * 4
)
PROMPTS = [f"Note {i}: {FILLER} Acknowledge in one word." for i in range(1, 7)]


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY — UI compaction e2e skipped.")
        return 0
    if not os.path.isfile(os.path.join(REPO, "web", "dist", "index.html")):
        print("SKIP: web/dist not built — run `npm --prefix web run build`.")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright not available.")
        return 0

    wks = tempfile.mkdtemp(prefix="compui-wks-")
    data_dir = tempfile.mkdtemp(prefix="compui-art-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_SERVE_UI": "1",
        "ADK_CC_WORKSPACE_ROOT": wks,
        "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{data_dir}",
        "ADK_CC_COMPACTION_TOKEN_THRESHOLD": "800",
        "ADK_CC_COMPACTION_EVENT_RETENTION": "3",
        "ADK_CC_COMPACTION_INTERVAL": "1",
        "ADK_CC_COMPACTION_OVERLAP": "1",
        "ADK_CC_MAX_CONTEXT_TOKENS": "200000",
        "ADK_CC_TOOL_TITLES": "0",
        # all summary-shaping tiers on, so the rendered divider reflects them
        "ADK_CC_MEMORY": "1",
        "ADK_CC_COMPACTION_SEED_MEMORY": "1",
    })
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

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(BASE, wait_until="networkidle")

            # AuthGate auto-signs-in (no-auth). Wait for the app picker, then
            # create a session.
            page.wait_for_selector("select", timeout=15000)
            # the app <select> auto-selects adk_cc; create a session
            page.click('button[title="New session"]', timeout=15000)
            page.wait_for_selector("textarea", timeout=15000)

            def send(text: str) -> None:
                ta = page.locator("textarea")
                ta.click()
                ta.fill(text)
                ta.press("Enter")

            def wait_idle(timeout_ms: int = 120000) -> None:
                # "agent is working…" shows while streaming; wait it out.
                try:
                    page.wait_for_selector("text=agent is working", timeout=8000)
                except Exception:
                    pass
                try:
                    page.wait_for_selector("text=agent is working", state="hidden",
                                           timeout=timeout_ms)
                except Exception:
                    pass

            found = False
            for i, prompt in enumerate(PROMPTS, 1):
                send(prompt)
                wait_idle()
                time.sleep(4)  # pace + let the post-turn compaction event land
                # the compaction event for turn N typically arrives on turn N+1;
                # check after each turn
                divider = page.locator("text=Context compacted")
                if divider.count() > 0:
                    found = True
                    print(f"  divider appeared after {i} turn(s)")
                    break
                print(f"  turn {i}: no divider yet")

            # one extra nudge turn — compaction is post-invocation, so the
            # marker may need the following turn to surface.
            if not found:
                send("Thanks, summarize what you remember in one line.")
                wait_idle()
                time.sleep(4)
                found = page.locator("text=Context compacted").count() > 0
                print(f"  after nudge turn: divider {'YES' if found else 'no'}")

            checks = {}
            if found:
                # expand it and check the panel + P5 footer render
                page.locator("text=Context compacted").first.click()
                time.sleep(1)
                checks["divider expands to summary panel"] = (
                    page.locator("text=Summary kept in place").count() > 0)
                # P5 footer (static, always present once expanded)
                checks["P5 footer renders"] = (
                    page.locator("text=stands in for the older messages").count() > 0)
                # P5 framing line should appear in the rendered summary text
                body = page.content().lower()
                checks["P5 framing line in summary"] = (
                    "continue the conversation directly" in body)
                for name, cond in checks.items():
                    print(f"  [{'PASS' if cond else 'WARN'}] {name}")
                ok = checks.get("divider expands to summary panel", False) \
                    and checks.get("P5 footer renders", False)
            page.screenshot(path=SHOT, full_page=True)
            print(f"  screenshot: {SHOT}")
            browser.close()

        print(f"\n  [{'PASS' if ok else 'FAIL'}] CompactionDivider renders (+ P5 footer)")
        print("compaction UI e2e " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        proc.kill()
        for d in (wks, data_dir):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
