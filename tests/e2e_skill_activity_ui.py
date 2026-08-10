"""E2E: a skill activation is legible in the thread, with a live model.

Two of the ecosystem's standing complaints were reproduced in adk-cc's own UI:
you could not tell WHICH skill the model chose, and you never saw what loading
one cost — a `load_skill` rendered as an anonymous wrench row like any other
tool call. This drives a real turn, with a real model, and reads the DOM.

Live because the point is what a model's own choice looks like on screen. Skips
cleanly without a model endpoint.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_skill_activity_ui.py
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8943
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + str(detail)) if detail and not ok else ''}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0
    endpoints = os.path.expanduser(
        "~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(endpoints):
        print("SKIP: no model endpoint registry to borrow."); return 0
    dist = os.path.join(REPO, "web", "dist-desktop")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print("SKIP: web UI not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    data = tempfile.mkdtemp(prefix="skillact-")
    proj = tempfile.mkdtemp(prefix="skillact-proj-")
    subprocess.run(["git", "init", "-q", proj], capture_output=True)
    # A dataset with an obvious driver: the task below is one the `data-analyst`
    # built-in is for, and it fired on every acceptance run. The skill is still
    # never named — the model choosing it is what produces the row under test.
    import random

    rnd = random.Random(7)
    rows = ["pressure,humidity,lot,defect_rate"]
    for _ in range(120):
        p_ = rnd.uniform(1, 9)
        rows.append(f"{p_:.2f},{rnd.uniform(20, 80):.1f},{rnd.randint(1, 9)},"
                    f"{2.1 * p_ + rnd.gauss(0, 0.4):.3f}")
    with open(os.path.join(proj, "data.csv"), "w") as fh:
        fh.write("\n".join(rows) + "\n")

    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_MODEL_REGISTRY_FILE": endpoints,
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SERVE_UI": "1", "ADK_CC_UI_DIST": dist,
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env,
        stdout=open(os.path.join(data, "server.log"), "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)

        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        sid = "s-activity"
        sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
        requests.post(sess, json={}, timeout=30)
        requests.patch(sess, json={"state_delta": {
            "model_endpoint": "chatgpt-codex", "model_id": MODEL}}, timeout=30)

        # A task the model should answer by reaching for a built-in skill. The
        # skill is never named: choosing it is what produces the row.
        t = requests.post(f"{BASE}/api/turns", timeout=60, json={
            "appName": "adk_cc", "userId": pid, "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text":
                "data.csv has one factor that actually drives defect_rate and "
                "two that do not. Tell me which one, and show me why you are "
                "confident."}]}}).json()
        for _ in range(200):
            time.sleep(3)
            st = requests.get(f"{BASE}/api/turns/{t['turn_id']}", timeout=30).json()
            if st["status"] != "running":
                break
        print(f"    turn: {st.get('status')}")

        events = requests.get(sess, timeout=30).json()["events"]
        loaded = [
            (p.get("functionCall") or {}).get("args", {}).get("skill_name")
            for e in events
            for p in ((e.get("content") or {}).get("parts") or [])
            if (p.get("functionCall") or {}).get("name") in (
                "load_skill", "run_skill_script")
        ]
        loaded = [s for s in loaded if s]
        if not loaded:
            print("    (the model used no skill this run — nothing to render)")
            print(f"\n{_passed} passed, {_failed} failed")
            return 0
        print(f"    skills touched: {sorted(set(loaded))}")
        # A script that failed is worth reading here: this harness is the only
        # place a REAL model drives a real skill script end to end.
        for e in events:
            for pt in ((e.get("content") or {}).get("parts") or []):
                fr = pt.get("functionResponse") or {}
                if fr.get("name") != "run_skill_script":
                    continue
                resp = fr.get("response") or {}
                if resp.get("status") == "error" or resp.get("error"):
                    tail = str(resp.get("stderr") or resp.get("error") or "")[-300:]
                    print(f"    script error: {' '.join(tail.split())}")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            # The rail starts collapsed: expand the project, THEN open its
            # session. Navigating straight to a ?project= URL left both closed
            # and the thread empty, which reads exactly like a missing card.
            proj_row = page.get_by_text(os.path.basename(proj), exact=False).first
            if proj_row.count() > 0:
                proj_row.click()
                page.wait_for_timeout(1200)
            # Session rows carry no id in the DOM — they are titled ("New Chat"
            # until the title plugin renames them), so click the first row of
            # the expanded project rather than matching on the session id.
            row = page.locator(".adk-session-row").first
            if row.count() > 0:
                row.click()
            page.wait_for_timeout(3000)
            page.screenshot(path=os.path.join(data, "opened.png"), full_page=True)

            # If anything folded the rows, open it: the assertions below are
            # about whether the SKILL row exists and reads correctly.
            grp = page.get_by_role("button", name="tool calls").first
            if grp.count() == 0:
                grp = page.locator("text=tool calls").first
            if grp.count() > 0:
                grp.click()
                page.wait_for_timeout(1500)
            card = page.locator("[data-skill-call]")
            check("the skill activation renders as its own row",
                  card.count() > 0, "(no skill row in the thread)")
            text = " ".join(card.first.inner_text().split()) if card.count() else ""
            check("the row names the skill the model chose",
                  any(s in text for s in set(loaded)), text[:160])
            check("and says what it cost or what it did",
                  ("tokens" in text or "ok" in text or "loaded" in text), text[:160])
            # Unfolded, the card must show BOTH halves — the call and the
            # response. It used to render `response ?? args`, so once a
            # result landed the call it answered became unreadable.
            if card.count() > 0:
                card.first.click()
                page.wait_for_timeout(600)
                # lower(): the section labels are CSS-uppercased and
                # inner_text() reports the rendered casing.
                body = " ".join(
                    card.first.locator("xpath=..").inner_text().split()).lower()
                check("unfolded: the call section is present",
                      "call ·" in body, body[:200])
                check("unfolded: the call args are readable",
                      '"skill_name"' in body, body[:200])
                check("unfolded: the response section is present too",
                      "response {" in body, body[:200])
            # The thread scrolls internally, so a full-page shot lands on the
            # final answer; put the row on screen before capturing it.
            if card.count() > 0:
                card.first.scroll_into_view_if_needed()
                page.wait_for_timeout(400)
            page.screenshot(path=os.path.join(data, "thread.png"))
            browser.close()
        print(f"    screenshot: {data}/thread.png")
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
