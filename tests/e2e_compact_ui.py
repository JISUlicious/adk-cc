"""Live UI e2e (#128): /compact from the composer, guided, on a real session.

Full pipeline: real model turns build up session events, then the user types
`/compact keep the magic word` in the composer. The slash menu must stay
matched while the guide is typed, Enter must dispatch (not send a message),
the server compacts the quiescent session, and the notice reports it. A
follow-up REAL turn then proves the compacted session still works AND that
the model still knows the magic word (the summary — LLM or mechanical —
carries it, or the retained tail does).

Web shell (both shells share ChatPage/Composer and the /api/compact route):

  WEB=1 ADK_CC_LIVE=1 .venv/bin/python tests/e2e_compact_ui.py

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
PORT = 8949
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
MAGIC = "quokka-88"

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

    data = tempfile.mkdtemp(prefix="compactui-")
    wsroot = tempfile.mkdtemp(prefix="compactui-wsroot-")

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

            def turn(text, expect, tries=60):
                box.fill(text)
                page.get_by_title("Send (Enter)").click()
                for _ in range(tries):
                    page.wait_for_timeout(2000)
                    if page.get_by_text(expect, exact=False).count() > 0:
                        return True
                return False

            # Build history: the magic word enters the transcript, then two
            # filler turns push it toward the compactable head.
            check("turn 1: model acknowledges the magic word",
                  turn(f"The magic word is {MAGIC}. Reply with just: noted "
                       f"{MAGIC}", MAGIC))
            check("turn 2 completes",
                  turn("Reply with just the word: alpha", "alpha"))
            check("turn 3 completes",
                  turn("Reply with just the word: bravo", "bravo"))
            check("turn 4 completes",
                  turn("Reply with just the word: charlie", "charlie"))

            # The visible reply lands before the server-side turn settles
            # (post-turn capture, title). The client retries 409s, but give
            # the API a head start so the e2e also passes on slow captures.
            try:
                sess = requests.get(
                    f"{BASE}/apps/adk_cc/users/alice/sessions", timeout=10
                ).json()
                sid = (sess[0].get("id") if isinstance(sess, list) and sess
                       else None)
                for _ in range(60):
                    t = requests.get(
                        f"{BASE}/api/turns/latest?appName=adk_cc&userId=alice"
                        f"&sessionId={sid}", timeout=10)
                    if not t.ok or t.json().get("status") != "running":
                        break
                    time.sleep(1)
            except Exception:
                pass

            # /compact with a guide: the menu row must stay matched while the
            # guide is typed, and Enter dispatches the ACTION (no message).
            box.fill("/compact keep the magic word and who said it")
            page.wait_for_timeout(400)
            menu_row = page.get_by_text(
                "Summarize this session's history", exact=False)
            check("slash menu still matched with a guide typed",
                  menu_row.count() > 0)
            box.press("Enter")

            noticed = False
            for _ in range(100):  # 409 retries + a 60s summarizer cap
                page.wait_for_timeout(1000)
                if page.get_by_text("Compacted", exact=False).count() > 0:
                    noticed = True
                    break
            page.screenshot(path=os.path.join(data, "compacted.png"))
            check("notice reports the compaction", noticed)
            check("notice shows the guide was applied",
                  page.get_by_text("guide applied", exact=False).count() > 0)
            check("no stray '/compact …' message entered the thread",
                  page.get_by_text("/compact keep the magic",
                                   exact=False).count() == 0)

            # The compacted session must still run turns — and still know
            # the magic word (summary or retained tail carries it).
            check("post-compaction turn: model still recalls the magic word",
                  turn("What was the magic word from earlier? Reply with "
                       "just the word.", MAGIC, tries=90))
            page.screenshot(path=os.path.join(data, "after.png"))
            browser.close()
        print(f"    screenshots: {data}/compacted.png, {data}/after.png")
        print(f"    server log:  {data}/server.log")
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
