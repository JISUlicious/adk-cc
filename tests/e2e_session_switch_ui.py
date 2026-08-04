"""Opening a NEW session while another is working, in the running desktop app.

Reported from a live remote run: "new session while working on other session
makes it unusable, showing agent is working…". The new chat is not running
anything — it inherits the OLD session's streaming state and never clears it,
so the composer stays disabled and the thread claims the agent is busy.

Cause is client-side. The session-change effect only ever set `isStreaming`
to TRUE (when the session being opened has a live turn); nothing set it back
to false, and nothing detached the previous session's SSE stream. So:

  * the new session shows "agent is working…" forever, and
  * the OLD turn's events keep arriving and are appended into the NEW
    session's thread — cross-session bleed, which is worse than the freeze
    because it looks like real output.

A unit test cannot see either: both live in React state driven by a real SSE
stream. So this drives the actual app with a genuinely slow turn in flight.

The model is asked to run a long shell command, which keeps turn A running
while we switch away — no mocking of the stream, and the turn stays durable
server-side (switching must not abort it).

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_session_switch_ui.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8971
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

    data = tempfile.mkdtemp(prefix="switchui-data-")
    home = tempfile.mkdtemp(prefix="switchui-home-")
    proj = tempfile.mkdtemp(prefix="switchui-proj-")
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

    env = dict(os.environ)
    env.update({
        "ADK_CC_MODEL_REGISTRY_FILE": os.path.expanduser(
            "~/.adk-cc-desktop/admin-data/model-endpoints.json"),
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

            # --- session A: start a turn that will still be running later ---
            composer = page.locator("textarea").first
            composer.click()
            composer.fill(
                "Run this exact shell command with run_bash and report its "
                "output: `sleep 45 && echo done-sleeping`")
            page.keyboard.press("Enter")
            page.wait_for_timeout(6000)

            sessions = requests.get(sess_api, timeout=30).json()
            sid_a = sessions[-1]["id"]
            turn_a = requests.get(
                f"{BASE}/api/turns/latest?appName=adk_cc&userId={pid}"
                f"&sessionId={sid_a}", timeout=15)
            check("session A has a turn running",
                  turn_a.ok and turn_a.json().get("status") == "running",
                  turn_a.text[:200])

            body = page.inner_text("body")
            check("session A shows the working indicator (as it should)",
                  "agent is working" in body.lower(), body[-200:])

            rows_before = page.locator(".adk-session-title").count()

            # --- open a NEW session while A is still running ---
            new_btn = page.locator(
                "[title='New session in this project'], "
                "[title='New session']").first
            new_btn.click(timeout=10000)
            page.wait_for_timeout(3500)

            # Asserted in the UI, not the API: a desktop chat is created
            # lazily and does not reach /sessions until its first message, so
            # comparing ids there compares A with itself.
            rows_after = page.locator(".adk-session-title").count()
            check("a second chat row appeared in the rail",
                  rows_after > rows_before, f"{rows_before} -> {rows_after}")

            # THE BUG: the new session claims to be working.
            body_b = page.inner_text("body")
            check("the NEW session does NOT claim the agent is working",
                  "agent is working" not in body_b.lower(),
                  body_b[-300:])

            # …and it must accept input rather than being frozen.
            composer_b = page.locator("textarea").first
            check("the NEW session's composer is enabled",
                  composer_b.is_enabled(), "composer disabled")

            # Cross-session bleed, asserted on CONTENT rather than on bubble
            # structure: A's prompt and its command output must not appear in
            # a session that never sent them.
            check("session A's content does not bleed into the new session",
                  "sleep 45" not in body_b and "done-sleeping" not in body_b,
                  body_b[-300:])

            # Switching away must DETACH, not abort: A keeps running.
            turn_a2 = requests.get(
                f"{BASE}/api/turns/latest?appName=adk_cc&userId={pid}"
                f"&sessionId={sid_a}", timeout=15).json()
            check("session A's turn survives the switch (durable, not aborted)",
                  turn_a2.get("status") in ("running", "done"),
                  turn_a2.get("status"))

            # --- and going back re-attaches A ---
            # Find A by its CONTENT: the new empty chat now sits at the top of
            # the rail, so "first row" is no longer session A.
            titles = page.locator(".adk-session-title")
            back = ""
            for i in range(titles.count()):
                titles.nth(i).click(timeout=10000)
                page.wait_for_timeout(2500)
                back = page.inner_text("body")
                if "sleep 45" in back:
                    break
            still_running = requests.get(
                f"{BASE}/api/turns/latest?appName=adk_cc&userId={pid}"
                f"&sessionId={sid_a}", timeout=15).json().get("status")
            if still_running == "running":
                check("returning to A re-attaches its live turn",
                      "agent is working" in back.lower(), back[-200:])
            else:
                print("  [skip] A finished before we returned — re-attach "
                      "not exercised")
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
