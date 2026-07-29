"""W6.3: a turn's outputs read as one run, in the chat and in the panel.

Live, because the thing under test is the path from real events (invocation_id
+ artifactDelta) to the DOM — a fixture would test my own event shape.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_run_view_ui.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8959
BASE = f"http://127.0.0.1:{PORT}"
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
    from playwright.sync_api import sync_playwright

    data = tempfile.mkdtemp(prefix="runview-")
    proj = os.path.join(data, "project")
    os.makedirs(proj, exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)

    env = dict(os.environ)
    for k in ("ADK_CC_API_KEY", "ADK_CC_SKIP_DOTENV", "ADK_CC_SKIP_CONFIG_CHECK"):
        env.pop(k, None)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": dist, "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SESSION_DSN": "sqlite:///" + os.path.join(data, "s.db"),
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
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=10).json()["project"]["id"]
        sid = f"run{int(time.time())}"
        base = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
        requests.post(base, json={}, timeout=30)
        requests.patch(base, json={"state_delta": {
            "model_endpoint": "chatgpt-codex",
            "model_id": "chatgpt-codex/gpt-5.4-mini",
            "permission_mode": "bypassPermissions"}}, timeout=30)
        t = requests.post(f"{BASE}/api/turns", timeout=60, json={
            "appName": "adk_cc", "userId": pid, "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text":
                "In analysis/, write FOUR small files: a.html and b.html (each a "
                "self-contained page with an inline-JS canvas bar chart), "
                "notes.md, and summary.md. Keep them tiny."}]}}).json()
        for _ in range(90):
            time.sleep(4)
            st = requests.get(f"{BASE}/api/turns/{t['turn_id']}", timeout=30).json()
            if st["status"] != "running":
                break
        sess = requests.get(base, timeout=30).json()
        deltas = [f for e in sess["events"]
                  for f in ((e.get("actions") or {}).get("artifactDelta") or {})]
        check("the turn produced 3+ registered outputs", len(set(deltas)) >= 3,
              f"{sorted(set(deltas))}")

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.locator(".adk-project-row").first.click(timeout=8000)
            page.wait_for_timeout(2500)
            rows = page.locator(".adk-session-title")
            for i in range(rows.count()):
                rows.nth(i).click(timeout=4000)
                page.wait_for_timeout(3000)
                if "outputs from this run" in page.inner_text("body"):
                    break
            text = page.inner_text("body")

            check("the chat collapses them into one run card",
                  "outputs from this run" in text, text[-300:])
            check("the run panel lists the run", "Runs" in text, text[:300])
            # the prompt, not the filename, labels the run
            check("the run is labelled by what was asked",
                  "analysis/" in text and "write FOUR" in text.replace("\n", " "),
                  "run label missing the prompt")
            page.screenshot(path=os.path.join(data, "runs.png"), full_page=True)
            print(f"    screenshot: {os.path.join(data, 'runs.png')}")
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
