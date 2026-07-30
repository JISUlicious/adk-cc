"""Answer a question by CLICKING it, and check the agent keeps going.

`e2e_ask_question_resume.py` covers the same fix at the API level. This one
exists because the UI path has diverged from the API path twice in one day: a
turn started over /api/turns runs without the browser rendering it, and the UI
adds a 409 retry around the same endpoint. The reported symptom was a person
clicking Submit and watching nothing happen, so that is what this drives.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_ask_question_ui.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8964
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    dist = os.path.join(REPO, "web", "dist-desktop")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print("SKIP: desktop UI not built."); return 0
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs a live model turn (ADK_CC_LIVE=1)."); return 0
    endpoints = os.path.expanduser(
        "~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(endpoints):
        print("SKIP: no model endpoint registry to borrow."); return 0
    from playwright.sync_api import sync_playwright

    data = tempfile.mkdtemp(prefix="askui-")
    proj = os.path.join(data, "project")
    os.makedirs(proj, exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)

    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_MODEL_REGISTRY_FILE": endpoints,
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": dist, "ADK_CC_SANDBOX_BACKEND": "noop",
    })
    server_log = os.path.join(data, "server.log")
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=open(server_log, "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.locator(".adk-project-row").first.click(timeout=15000)
            page.wait_for_timeout(2000)
            rows = page.locator(".adk-session-title")
            if rows.count():
                rows.first.click(timeout=6000)
            page.wait_for_timeout(1500)

            listed = requests.get(f"{BASE}/apps/adk_cc/users/{pid}/sessions",
                                  timeout=30).json()
            sid = listed[-1]["id"]
            requests.patch(f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}",
                           json={"state_delta": {"model_endpoint": "chatgpt-codex",
                                                 "model_id": MODEL}}, timeout=30)

            box = page.locator(".adk-composer-input")
            stop = page.locator('button[title="Stop the streaming response"]')
            box.click()
            box.fill("I can't decide how to format this project. Ask me a "
                     "multiple-choice question about which style I want, and "
                     "wait for my answer.")
            page.keyboard.press("Enter")

            submit = page.get_by_role("button", name="Submit answers")
            deadline = time.time() + 300
            while time.time() < deadline:
                page.wait_for_timeout(700)
                if submit.count():
                    break
            check("the question card appears", submit.count() > 0,
                  "no question was asked — nothing to answer")
            if not submit.count():
                page.screenshot(path=os.path.join(data, "no-card.png"), full_page=True)
                print(f"    artifacts: {data}"); return 0

            before = page.inner_text("body")
            card = submit.first.locator(
                "xpath=ancestor::div[contains(@class,'bg-brand-tint')][1]")
            groups = card.locator("div.space-y-2")
            for gi in range(groups.count()):
                opt = groups.nth(gi).locator("button").first
                if opt.count():
                    opt.click()
                    page.wait_for_timeout(150)
            submit.first.click()
            print("    clicked Submit answers")

            # Wait for the follow-up turn to run and settle.
            started = False
            deadline = time.time() + 240
            while time.time() < deadline:
                page.wait_for_timeout(700)
                if stop.count():
                    started = True
                elif started:
                    break
            page.wait_for_timeout(2500)

            after = page.inner_text("body")
            check("the agent streamed something after the click", started,
                  "no streaming indicator ever appeared — the click did nothing")
            # NOT total body length: answering REPLACES the verbose question card
            # with a compact "ask_user_question finished" row, so the page
            # legitimately shrinks (773 → 424 chars in the first run) even though
            # the reply arrived. Look for a coordinator bubble instead.
            bubbles = page.locator("text=COORDINATOR")
            check("a coordinator reply is on screen", bubbles.count() > 0,
                  "no COORDINATOR bubble rendered after the answer")
            check("the broker's internal nudge is not shown as the user's message",
                  "Continue." not in after,
                  "a 'Continue.' bubble the user never typed is visible")
            page.screenshot(path=os.path.join(data, "answered.png"), full_page=True)

            events = requests.get(
                f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}", timeout=30
            ).json()["events"]
            idx = 0
            for i, e in enumerate(events):
                for p in ((e.get("content") or {}).get("parts") or []):
                    if (p.get("functionResponse") or {}).get("name") == "ask_user_question":
                        idx = i
            replies = [
                " ".join(p["text"].split())
                for e in events[idx + 1:] if (e.get("author") or "") != "user"
                for p in ((e.get("content") or {}).get("parts") or [])
                if p.get("text") and not p.get("thought")
            ]
            check("a model reply is recorded after the answer", bool(replies),
                  "session has no model text after the functionResponse")
            if replies:
                print(f"    reply: {replies[0][:120]}")
            print(f"    artifacts: {data}")
            b.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
