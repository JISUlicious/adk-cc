"""E2E: tool-call titles, end to end INCLUDING the web UI.

Two layers:

  A (deterministic, no model): seed a session whose event log carries tool
    calls with `title` args, mount the REAL React bundle in a REAL browser,
    and assert the titles render in the tool-card headers (BashTerminalCard
    + generic ToolCard), with the raw command still visible.

  B (live model + plugin): with ADK_CC_TOOL_TITLES=1 and the real model from
    .env, POST /run with a query forcing a titled run_bash call. Assert the
    recorded functionCall args carry the title (the plugin injected the param
    and the model filled it), the tool still executed (title stripped before
    execution), AND the title shows up in the web UI for that live session.
    SKIPs gracefully on model errors (rate limits) — part A already proved
    the rendering; part B proves the model-to-UI pipeline.

Runs its own ephemeral server on :8013 (your :8000 untouched). Builds
web/dist and restores the previous bundle afterwards.

    .venv/bin/python tests/e2e_tool_titles.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import requests
from playwright.sync_api import sync_playwright

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(REPO, "web")
PORT = 8013
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "tok"
USER = "alice"
APP = "adk_cc"

BASH_TITLE = "Greeting the world"
GREP_TITLE = "Hunting for needles"
LIVE_TITLE = "Echo greeting from e2e"
SEEDED_SESSION_TITLE = "Seeded rail title"
LIVE_SESSION_TITLE = "Fizzbuzz e2e session"

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    ok_all = ok_all and cond


def _build():
    env = dict(os.environ)
    env["VITE_ADK_CC_HTML_PREVIEW_ALLOW_SCRIPTS"] = "1"
    subprocess.run(["npm", "run", "build"], cwd=WEB, env=env, check=True,
                   capture_output=True)


def _start_server(data_dir):
    env = dict(os.environ)
    env.update({
        # .env loads the real model config (no SKIP_DOTENV); process env wins
        # for the overrides below (override=False in the loader).
        "ADK_CC_TOOL_TITLES": "1",
        "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_NOOP_ACK_HOST_EXEC": "1",
        "ADK_CC_AUTH_TOKENS": f"{TOKEN}={USER}:local",
        "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": os.path.join(WEB, "dist"),
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{data_dir}",
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"),
         "adk_cc.service.server:make_app", "--factory",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            requests.get(BASE + "/list-apps", timeout=2)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("server did not start")


def _hdr():
    return {"Authorization": f"Bearer {TOKEN}"}


def _seed_session() -> str:
    """run_bash pair + text + generic grep pair (text between them prevents
    Thread's ToolCallGroup from collapsing the two pairs into one group)."""
    events = [
        {"id": "e1", "author": "agent", "invocationId": "i1",
         "content": {"role": "model", "parts": [
             {"functionCall": {"id": "fc-bash", "name": "run_bash",
              "args": {"command": "echo hi", "title": BASH_TITLE}}}]}},
        {"id": "e2", "author": "agent", "invocationId": "i1",
         "content": {"role": "user", "parts": [
             {"functionResponse": {"id": "fc-bash", "name": "run_bash",
              "response": {"status": "ok", "command": "echo hi",
                           "exit_code": 0, "stdout": "hi\n", "stderr": ""}}}]}},
        {"id": "e3", "author": "agent", "invocationId": "i1",
         "content": {"role": "model", "parts": [{"text": "ran the greeting"}]}},
        {"id": "e4", "author": "agent", "invocationId": "i1",
         "content": {"role": "model", "parts": [
             {"functionCall": {"id": "fc-grep", "name": "grep",
              "args": {"pattern": "needle", "title": GREP_TITLE}}}]}},
        {"id": "e5", "author": "agent", "invocationId": "i1",
         "content": {"role": "user", "parts": [
             {"functionResponse": {"id": "fc-grep", "name": "grep",
              "response": {"status": "ok", "hits": []}}}]}},
    ]
    r = requests.post(f"{BASE}/apps/{APP}/users/{USER}/sessions",
                      headers=_hdr(),
                      json={"events": events,
                            "state": {"session_title": SEEDED_SESSION_TITLE}},
                      timeout=10)
    r.raise_for_status()
    return r.json()["id"]


def _open_session(pg, sid):
    pg.goto(BASE + "/")
    pg.evaluate(
        "(t)=>{localStorage.setItem('adk_cc.token',t);"
        "localStorage.setItem('adk_cc.user','alice')}", TOKEN)
    pg.reload()
    pg.wait_for_load_state("networkidle")
    pg.get_by_text(sid[:18]).first.click()
    pg.wait_for_timeout(600)


def part_a(browser) -> None:
    sid = _seed_session()
    print(f"A: seeded session {sid[:18]}")
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    _open_session(pg, sid)
    bash_title = pg.get_by_text(BASH_TITLE)
    grep_title = pg.get_by_text(GREP_TITLE)
    check("A: bash card header shows the title",
          bash_title.count() >= 1, f"'{BASH_TITLE}'")
    check("A: generic tool card shows the title",
          grep_title.count() >= 1, f"'{GREP_TITLE}'")
    # raw command still visible (secondary chip and/or terminal block)
    check("A: raw command still visible", pg.get_by_text("echo hi").count() >= 1)
    check("A: session rail shows the seeded session title",
          pg.get_by_text(SEEDED_SESSION_TITLE).count() >= 1,
          f"'{SEEDED_SESSION_TITLE}'")
    pg.close()


def part_b(browser) -> None:
    sid = requests.post(f"{BASE}/apps/{APP}/users/{USER}/sessions",
                        headers=_hdr(), json={}, timeout=10).json()["id"]
    print(f"B: live session {sid[:18]} — running real model (slow)…")
    r = requests.post(
        f"{BASE}/run", headers={**_hdr(), "Content-Type": "application/json"},
        json={"appName": APP, "userId": USER, "sessionId": sid,
              "newMessage": {"role": "user", "parts": [{"text":
                  "Use run_bash to run exactly `echo hello`. On that run_bash "
                  f"call, set the optional `title` argument to exactly: "
                  f"{LIVE_TITLE}. Also call set_session_title with the "
                  f"label exactly: {LIVE_SESSION_TITLE}. Then tell me the "
                  "output."}]}},
        timeout=420,
    )
    if r.status_code != 200:
        print(f"  [SKIP] B: /run returned {r.status_code} (model error / rate "
              f"limit) — deterministic part A already covers rendering")
        return
    events = r.json()
    titled = [
        p["functionCall"] for e in events
        for p in (e.get("content") or {}).get("parts") or []
        if p.get("functionCall", {}).get("name") == "run_bash"
        and isinstance(p["functionCall"].get("args", {}).get("title"), str)
    ]
    check("B: live functionCall args carry a title",
          bool(titled), titled[0]["args"].get("title") if titled else "none")
    executed = [
        p["functionResponse"] for e in events
        for p in (e.get("content") or {}).get("parts") or []
        if p.get("functionResponse", {}).get("name") == "run_bash"
        and (p["functionResponse"].get("response") or {}).get("status") == "ok"
    ]
    check("B: run_bash still executed ok (title stripped before tool)",
          bool(executed))
    sessions = requests.get(f"{BASE}/apps/{APP}/users/{USER}/sessions",
                            headers=_hdr(), timeout=10).json()
    mine = next((s for s in sessions if s["id"] == sid), {})
    check("B: state.session_title set by the model",
          (mine.get("state") or {}).get("session_title") == LIVE_SESSION_TITLE,
          repr((mine.get("state") or {}).get("session_title")))
    # the titles must also render in the real UI for this live session
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    _open_session(pg, sid)
    shown = pg.get_by_text(LIVE_TITLE).count() >= 1
    if not titled:
        print("  [SKIP] B: UI check (model set no title)")
    else:
        check("B: live title renders in the web UI", shown, f"'{LIVE_TITLE}'")
    check("B: session title renders in the rail",
          pg.get_by_text(LIVE_SESSION_TITLE).count() >= 1,
          f"'{LIVE_SESSION_TITLE}'")
    pg.close()


def main() -> int:
    backup, dist = os.path.join(WEB, "dist.titles-backup"), os.path.join(WEB, "dist")
    if os.path.isdir(dist):
        if os.path.isdir(backup):
            shutil.rmtree(backup)
        shutil.move(dist, backup)
    data_dir = os.path.join(REPO, ".workspace", "tool-titles-e2e")
    if os.path.isdir(data_dir):
        shutil.rmtree(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    proc = None
    try:
        print("building bundle…")
        _build()
        proc = _start_server(data_dir)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            part_a(browser)
            part_b(browser)
            browser.close()
        print("\ntool-titles e2e " + ("PASSED" if ok_all else "FAILED"))
        return 0 if ok_all else 1
    finally:
        if proc:
            proc.kill()
        if os.path.isdir(backup):
            if os.path.isdir(dist):
                shutil.rmtree(dist)
            shutil.move(backup, dist)


if __name__ == "__main__":
    sys.exit(main())
