"""Sending while the session is BUSY: the 409 must be waited out, not lost.

Single-flight means a second POST /api/turns for a session that already has a
running turn gets a 409. That is correct server behaviour; what matters is
what the CLIENT does with it.

It used to retry 20 x 500ms and then surface the error. Ten seconds suits the
out-of-band title call and nothing else: a real tool holds the turn for
minutes, so a user answering a confirmation — or just sending again — had
their message DROPPED with a bare "409 conflict" and no way to resend. That
is the same lost-input class as the dropped messages in #106.

This forces the conflict for real: start a turn that runs ~45s, then send
another message into the SAME session while it is still going. With the fix
the second message waits and lands; with the old fixed budget it dies at 10s.

No mocking — a real model, a real slow tool, the real single-flight path.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_busy_409_ui.py
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8975
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

            # --- a turn that will still be running when we send again ---
            composer = page.locator("textarea").first
            composer.click()
            composer.fill(
                "Run this exact shell command with run_bash and report its "
                "output: `sleep 45 && echo first-done`")
            page.keyboard.press("Enter")
            page.wait_for_timeout(6000)

            sessions = requests.get(sess_api, timeout=30).json()
            sid = sessions[-1]["id"]
            latest = f"{BASE}/api/turns/latest?appName=adk_cc&userId={pid}&sessionId={sid}"
            t1 = requests.get(latest, timeout=15)
            check("the first turn is running (single-flight is engaged)",
                  t1.ok and t1.json().get("status") == "running", t1.text[:200])

            # --- send again INTO THE SAME SESSION while it is busy ---
            composer = page.locator("textarea").first
            composer.click()
            composer.fill("SECOND-MESSAGE-MARKER: what is 2+2?")
            page.keyboard.press("Enter")
            page.wait_for_timeout(4000)

            # The old client would already have given up around here (10s).
            body_busy = page.inner_text("body")
            check("no 409 is shown while the session is busy",
                  "409" not in body_busy and "conflict" not in body_busy.lower(),
                  body_busy[-300:])

            # Measured here, and it changes what this test can claim: the
            # composer REFUSES to send while a turn streams
            # (Composer.tsx: `if (!trimmed || disabled || isStreaming) return`
            # — it shows Stop instead of Send). So a plain send can never
            # reach the busy path at all. It is also not lost: the text stays
            # in the box for the user to send when the turn ends.
            still_typed = page.locator("textarea").first.input_value()
            check("a send during a live turn is refused, not swallowed",
                  "SECOND-MESSAGE-MARKER" in still_typed,
                  f"textarea={still_typed[:80]!r}")

            for _ in range(90):
                page.wait_for_timeout(2000)
                if "first-done" in page.inner_text("body"):
                    break
            final = page.inner_text("body")
            check("the first turn completed", "first-done" in final,
                  final[-300:])
            check("no 409 was ever surfaced",
                  "409" not in final and "conflict" not in final.lower(),
                  final[-400:])

            # …and once the turn ends, the same text sends normally. Wait for
            # the composer to swap Stop back to Send — that button IS the
            # isStreaming state, so this also checks the UI leaves the
            # streaming state when the turn actually ends.
            send_btn = page.locator("[title='Send (Enter)']")
            try:
                send_btn.first.wait_for(state="visible", timeout=60000)
                back_to_send = True
            except Exception:
                back_to_send = False
            check("the composer returns to Send when the turn ends",
                  back_to_send, "still showing Stop")
            page.wait_for_timeout(1000)
            page.locator("textarea").first.click()
            page.keyboard.press("Enter")
            landed = False
            for _ in range(45):
                page.wait_for_timeout(2000)
                events = requests.get(
                    f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}",
                    timeout=30).json().get("events") or []
                if "SECOND-MESSAGE-MARKER" in json.dumps(events)[:400000]:
                    landed = True
                    break
            check("the held message sends once the turn is over", landed,
                  "marker never reached the transcript")
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
