"""Live UI e2e (#129-1): edit session notes from the /notes modal.

Full pipeline: /notes opens the modal, Edit → textarea → Save persists via
the state PATCH, reopening shows the edited text, the API confirms the
state key, and a REAL model turn proves the edited note is injected — the
model answers with a magic word that exists ONLY in the hand-edited notes
(never in any message or tool output).

Web shell (both shells share ChatPage; the PATCH route is shell-agnostic):

  WEB=1 ADK_CC_LIVE=1 .venv/bin/python tests/e2e_notes_edit_ui.py

Skips cleanly without a model endpoint / UI build / playwright.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8951
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
MAGIC = "pelican-64"
NOTE = (f"DECISION: the project codename is {MAGIC} — always answer with it "
        "when asked for the codename.")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + str(detail)) if detail and not ok else ''}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:  # noqa: PLR0915
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0
    endpoints = os.path.expanduser(
        "~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(endpoints):
        print("SKIP: no model endpoint registry to borrow."); return 0
    dist = os.path.join(REPO, "web", "dist")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print("SKIP: web UI not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    data = tempfile.mkdtemp(prefix="notesui-")
    wsroot = tempfile.mkdtemp(prefix="notesui-wsroot-")

    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_MODEL_REGISTRY_FILE": endpoints,
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_DATA_DIR": data,
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SERVE_UI": "1", "ADK_CC_UI_DIST": dist,
        "ADK_CC_DEFAULT_MODEL": MODEL,
        "ADK_CC_WORKSPACE_ROOT": wsroot,
        "ADK_CC_NOOP_ACK_HOST_EXEC": "1",
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

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 950})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            page.get_by_role("button", name="New").first.click(timeout=20000)
            page.wait_for_timeout(2000)

            box = page.locator("textarea.adk-composer-input")

            def open_notes():
                box.fill("/notes")
                page.wait_for_timeout(300)
                box.press("Enter")
                page.wait_for_timeout(500)

            # Open, edit, save.
            open_notes()
            check("modal opens empty",
                  page.locator("[data-session-notes]").count() > 0
                  and "no notes yet" in page.locator(
                      "[data-session-notes]").inner_text())
            page.get_by_role("button", name="Edit").click()
            edit = page.locator("[data-session-notes-edit]")
            check("edit mode: textarea appears", edit.count() > 0)
            edit.fill(NOTE)
            page.get_by_role("button", name="Save").click()
            page.wait_for_timeout(1500)
            check("view mode shows the saved note",
                  MAGIC in page.locator("[data-session-notes]").inner_text())

            # Close, reopen — persistence through a fresh modal.
            page.mouse.click(20, 20)  # backdrop
            page.wait_for_timeout(400)
            open_notes()
            check("reopened modal still shows the note",
                  MAGIC in page.locator("[data-session-notes]").inner_text())
            page.screenshot(path=os.path.join(data, "notes.png"))
            page.mouse.click(20, 20)
            page.wait_for_timeout(400)

            # API-level: the state key really holds the edit.
            try:
                sess = requests.get(
                    f"{BASE}/apps/adk_cc/users/alice/sessions", timeout=10
                ).json()
                sid = sess[0]["id"]
                full = requests.get(
                    f"{BASE}/apps/adk_cc/users/alice/sessions/{sid}",
                    timeout=10).json()
                check("API: session.state.session_notes == edit",
                      full.get("state", {}).get("session_notes") == NOTE,
                      str(full.get("state", {}).get("session_notes"))[:80])
            except Exception as e:  # noqa: BLE001
                check("API: session.state.session_notes == edit", False, e)

            # THE load-bearing check: a real turn answers from the note.
            # The magic word exists nowhere but the hand-edited notes.
            box.fill("What is the project codename? Reply with just the "
                     "codename.")
            page.get_by_title("Send (Enter)").click()
            answered = False
            for _ in range(90):
                page.wait_for_timeout(2000)
                # The modal is closed (unmounted), and no message contains
                # the magic word — any DOM hit is the model's reply.
                if page.get_by_text(MAGIC, exact=False).count() > 0:
                    answered = True
                    break
            check("live turn: model answers from the EDITED note", answered)
            page.screenshot(path=os.path.join(data, "answered.png"))
            browser.close()
        print(f"    screenshots: {data}/notes.png, {data}/answered.png")
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
