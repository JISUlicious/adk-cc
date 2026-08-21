"""Session titles, in the running desktop app.

Titling is an out-of-band model call spawned at before_run and persisted at
after_run (SessionTitlePlugin). It is exactly the kind of thing unit tests
bless and the product then fails at: the plugin can work perfectly while the
title never reaches `session.state`, or reaches it and never reaches the rail.

Also worth checking here specifically: the title call is what creates the
brief window in which a session is "busy" after its visible turn has ended —
the window that produced spurious 409s. So this asserts both that the title
appears AND that its background call does not block the next message.

Checks, against a real model:
  1. a fresh chat starts untitled ("New Chat" in the rail)
  2. after the first message, `state.session_title` is set to something
     derived from that message — not a placeholder, not the raw prompt
  3. the rail shows it without a reload
  4. a second message does not re-title the session
  5. sending again right after the first reply works (no 409 surfaced)

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_session_title_ui.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8973
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


def _state(pid: str, sid: str) -> dict:
    r = requests.get(f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}", timeout=30)
    return (r.json() or {}).get("state") or {}


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

    data = tempfile.mkdtemp(prefix="titleui-data-")
    home = tempfile.mkdtemp(prefix="titleui-home-")
    proj = tempfile.mkdtemp(prefix="titleui-proj-")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=False)
    open(os.path.join(proj, "README.md"), "w").write("# title test\n")
    for rel in (".codex", ".adk-cc-desktop"):
        src = os.path.expanduser(f"~/{rel}")
        if os.path.exists(src):
            try:
                os.symlink(src, os.path.join(home, rel))
            except OSError:
                pass

    env = dict(os.environ)
    # STRIP the tool-titles flag: titling used to be chained to it, and this
    # very test once passed only because the dev shell leaked it in. The
    # feature must work with NO flag — that is the fix under test (5c0eb99).
    env.pop("ADK_CC_TOOL_TITLES", None)
    # …and the repo .env ALSO sets it (line 674) — the server runs with
    # cwd=REPO and loads it, which is how an A/B against the pre-fix code
    # passed on both sides. Registration must be provable with NO flag from
    # ANY source.
    env["ADK_CC_SKIP_DOTENV"] = "1"
    env["ADK_CC_SKIP_CONFIG_CHECK"] = "1"
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

            rail = page.inner_text("body")
            check("a fresh chat is untitled in the rail",
                  "New Chat" in rail, rail[:200])

            # A prompt with an unmistakable subject, so a real title is
            # distinguishable from a generic one.
            prompt = ("What is the capital of Portugal? Answer in one word, "
                      "no tools.")
            composer = page.locator("textarea").first
            composer.click()
            composer.fill(prompt)
            page.keyboard.press("Enter")

            # Titling overlaps the turn and persists at after_run; give the
            # turn time to finish and the persist to land.
            sid = None
            title = ""
            # 120s, not 60: a loaded machine (concurrent builds) pushed the
            # live title past the old budget and the run flaked (#112).
            for _ in range(120):
                page.wait_for_timeout(1000)
                rows = requests.get(sess_api, timeout=30).json()
                if not rows:
                    continue
                sid = rows[-1]["id"]
                title = (_state(pid, sid).get("session_title") or "").strip()
                if title:
                    break

            print(f"    generated title: {title!r}")
            check("the session got a title", bool(title), f"sid={sid}")
            if title:
                check("the title is not a placeholder",
                      title.lower() not in ("new chat", "untitled", "chat"),
                      title)
                # It should summarise, not echo: the raw prompt back verbatim
                # is the failure mode when the title model no-ops.
                check("the title is a summary, not the prompt verbatim",
                      title.strip() != prompt.strip() and len(title) < 80,
                      f"{title!r}")
                check("the title relates to the message",
                      any(w in title.lower()
                          for w in ("portugal", "capital", "lisbon")),
                      f"{title!r}")

            # It must reach the rail on its own — the plugin persisting to
            # state is only half the feature.
            page.wait_for_timeout(6000)
            rail2 = page.inner_text("body")
            if title:
                check("the title shows in the rail without a reload",
                      title[:20] in rail2, rail2[:300])

            # A second message must not re-title an already-titled session.
            composer = page.locator("textarea").first
            composer.click()
            composer.fill("And the capital of Spain? One word.")
            page.keyboard.press("Enter")
            page.wait_for_timeout(25000)
            if title:
                title2 = (_state(pid, sid).get("session_title") or "").strip() if sid else ""
                check("a titled session is not re-titled", title2 == title,
                      f"{title!r} -> {title2!r}")
            else:
                # "" == "" would pass vacuously — with no title there is
                # nothing to protect from re-titling (#112).
                print("    (skip: no title was generated; re-title check is moot)")

            # The second send is also the 409 check: the title call runs
            # out-of-band and briefly outlives the visible turn, which is the
            # window that used to surface a spurious conflict.
            body = page.inner_text("body")
            check("no 409/conflict surfaced to the user",
                  "409" not in body and "conflict" not in body.lower(),
                  body[-300:])
            b.close()

        log = open(server_log, encoding="utf-8", errors="replace").read()
        check("no title-plugin errors in the server log",
              "session title" not in log.lower()
              or "error" not in log.lower().split("session title")[-1][:200],
              "see log")
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
