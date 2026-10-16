"""Mid-turn messages + send-now, through the REAL UI, a real model and a
real slow tool (#mid-turn queue).

  1. STEER: while `sleep 25` runs, Enter queues a message — a chip shows,
     the text box clears, the chip disappears when the agent reads it, the
     transcript carries it as a user event under the SAME invocation, and the
     agent acts on it (runs the extra command) without the user starting a
     new turn.
  2. SEND-NOW: while `sleep 90` runs, Ctrl+Enter stops the turn and the new
     message runs immediately; the slow command never completes.

No mocking. Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_midturn_ui.py
"""


from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8976
BASE = f"http://127.0.0.1:{PORT}"
MODEL = os.environ.get("MIDTURN_MODEL", "chatgpt-codex/gpt-6-luna")
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:  # noqa: PLR0915
    dist = os.path.join(REPO, "web", "dist-desktop")
    if not os.path.isdir(dist):
        print("SKIP: desktop UI not built."); return 0
    if not os.environ.get("ADK_CC_LIVE"):
        print("SKIP: set ADK_CC_LIVE=1 to run (drives a real model)."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed."); return 0

    data = tempfile.mkdtemp(prefix="midturn-data-")
    home = tempfile.mkdtemp(prefix="midturn-home-")
    proj = tempfile.mkdtemp(prefix="midturn-proj-")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=False)
    open(os.path.join(proj, "README.md"), "w").write("# switch test\n")

    # The subscription model path reads ~/.codex/auth.json; $HOME is redirected.
    for rel in (".codex", ".adk-cc-desktop"):
        src = os.path.expanduser(f"~/{rel}")
        if os.path.exists(src):
            try:
                os.symlink(src, os.path.join(home, rel))
            except OSError:
                pass

    # A temp COPY of the desktop registry with the codex endpoint pinned to
    # MODEL: the ChatGPT backend retired gpt-5.4-mini (HTTP 400) and the
    # user's live registry must not be edited by a test.
    reg_src = os.path.expanduser("~/.adk-cc-desktop/admin-data/model-endpoints.json")
    reg = json.load(open(reg_src))
    for ep in reg.get("endpoints", []):
        if str(ep.get("model", "")).startswith("chatgpt-codex/"):
            ep["model"] = MODEL
    reg_path = os.path.join(data, "model-endpoints.json")
    json.dump(reg, open(reg_path, "w"))
    env = dict(os.environ)
    env.update({
        "ADK_CC_MODEL_REGISTRY_FILE": reg_path,
        "HOME": home,
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": dist, "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_DEFAULT_MODEL": MODEL,
    })
    server_log = os.path.join(data, "server.log")
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=open(server_log, "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(160):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        sess_api = f"{BASE}/apps/adk_cc/users/{pid}/sessions"

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1400, "height": 950})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            page.locator(".adk-project-row").first.click(timeout=15000)
            page.wait_for_timeout(2500)

            composer = page.locator("textarea").first
            composer.click()
            composer.fill(
                "Run this exact shell command with run_bash and report its "
                "output: `sleep 25 && echo step-one-done`")
            page.keyboard.press("Enter")
            page.wait_for_timeout(7000)
            sid = requests.get(sess_api, timeout=30).json()[-1]["id"]
            latest = f"{BASE}/api/turns/latest?appName=adk_cc&userId={pid}&sessionId={sid}"
            t1 = requests.get(latest, timeout=15).json()
            check("turn 1 is running", t1.get("status") == "running", str(t1)[:200])

            # --- 1. STEER: queue a message mid-turn ------------------------
            composer = page.locator("textarea").first
            composer.click()
            composer.fill("MIDTURN-MARKER: when that finishes, ALSO run "
                          "`echo steer-ok` with run_bash and include its output.")
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
            chip = page.locator("[data-queued-item]")
            check("Enter while working queues it (chip shown)",
                  chip.count() == 1 and "MIDTURN-MARKER" in chip.first.inner_text(),
                  f"chips={chip.count()}")
            check("…and the text box is cleared, not refused",
                  page.locator("textarea").first.input_value() == "")
            for _ in range(60):
                page.wait_for_timeout(2000)
                if page.locator("[data-queued-item]").count() == 0:
                    break
            check("the chip clears when the agent reads it",
                  page.locator("[data-queued-item]").count() == 0)
            for _ in range(90):
                page.wait_for_timeout(2000)
                body = page.inner_text("body")
                if "steer-ok" in body and requests.get(latest, timeout=15).json().get("status") != "running":
                    break
            events = requests.get(f"{sess_api}/{sid}", timeout=30).json().get("events") or []
            mid = [e for e in events if e.get("author") == "user"
                   and (e.get("custom_metadata") or e.get("customMetadata") or {}).get("adk_cc_midturn")]
            first_inv = next((e.get("invocation_id") or e.get("invocationId")) for e in events
                             if e.get("author") == "user")
            check("delivered as a user event inside the SAME turn",
                  len(mid) == 1 and (mid[0].get("invocation_id") or mid[0].get("invocationId")) == first_inv,
                  f"midturn events={len(mid)}")
            blob = json.dumps(events)
            check("the agent acted on it (ran the extra command)",
                  "steer-ok" in blob and "step-one-done" in blob, blob[-300:])
            user_turns = [e for e in events if e.get("author") == "user"
                          and any(p.get("text") for p in (e.get("content") or {}).get("parts") or [])]
            check("no second turn was needed", len(user_turns) == 2, f"user text events={len(user_turns)}")

            # --- 2. SEND-NOW: Ctrl+Enter stops and sends ---------------------
            page.locator("[title='Send (Enter)']").first.wait_for(state="visible", timeout=60000)
            composer = page.locator("textarea").first
            composer.click()
            composer.fill("Run this exact shell command with run_bash: "
                          "`sleep 90 && echo slow-never-done`")
            page.keyboard.press("Enter")
            page.wait_for_timeout(9000)
            slow = requests.get(latest, timeout=15).json()
            check("the slow turn is running", slow.get("status") == "running")
            composer = page.locator("textarea").first
            composer.click()
            composer.fill("SENDNOW-MARKER: forget that. Reply with exactly: sendnow-ok")
            page.keyboard.press("Control+Enter")
            def _model_said(marker):
                evs = requests.get(f"{sess_api}/{sid}", timeout=30).json().get("events") or []
                return any(e.get("author") != "user" and marker in json.dumps(e.get("content") or {})
                           for e in evs)
            for _ in range(45):
                page.wait_for_timeout(2000)
                if _model_said("sendnow-ok"):
                    break
            st_old = requests.get(f"{BASE}/api/turns/{slow['turn_id']}", timeout=15).json()
            check("Ctrl+Enter stopped the running turn", st_old.get("status") == "aborted", st_old.get("status"))
            check("…and the new message ran right away", _model_said("sendnow-ok"))
            check("the slow command never completed", not _model_said("slow-never-done\n")
                  and not any("slow-never-done" in json.dumps((p.get("functionResponse") or {}).get("response") or {})
                              for e in requests.get(f"{sess_api}/{sid}", timeout=30).json().get("events") or []
                              for p in (e.get("content") or {}).get("parts") or []))
            page.screenshot(path=os.path.join(data, "midturn.png"), full_page=True)
            b.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n{_passed} passed, {_failed} failed")
    if _failed:
        print(f"server log: {server_log}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
