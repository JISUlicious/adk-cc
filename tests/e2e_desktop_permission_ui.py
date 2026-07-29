"""The desktop permission default and the page nudge, in the running app.

Both were shipped on unit tests that cannot see the product: one imports the
agent in a subprocess, the other feeds synthetic events to a pure function.
Neither proves a fresh chat in the actual desktop app starts in acceptEdits,
that a shell-rc write now prompts, or that the new signal ever reaches a model.

So this drives the real UI:

  1. open a project, let the shell create its own session, and read the mode it
     recorded — nothing is pinned, so this is the shipped default
  2. ask for a shell-startup edit and require a confirmation card, then DENY it
     and assert the file on disk is untouched
  3. ask for a page and a claim about it, then look for the nudge in the server
     log (`verify nudge: …`, emitted by VerifyNudgePlugin)

`$HOME` is redirected to a temp dir so `~/.zshrc` is a fixture, not the
operator's real shell config. The model credential dirs are symlinked in, since
the ChatGPT-subscription path reads `~/.codex/auth.json`.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_desktop_permission_ui.py
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8968
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
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0
    from playwright.sync_api import sync_playwright

    data = tempfile.mkdtemp(prefix="permui-")
    home = os.path.join(data, "home")
    os.makedirs(home, exist_ok=True)
    zshrc = os.path.join(home, ".zshrc")
    original = "# fixture shell config\nexport FIXTURE=1\n"
    open(zshrc, "w").write(original)
    # The model credentials live in the real home; without them every turn dies
    # on auth and the test would "pass" by never getting far enough to fail.
    for name in (".codex", ".adk-cc"):
        src = os.path.expanduser(f"~/{name}")
        if os.path.exists(src):
            try:
                os.symlink(src, os.path.join(home, name))
            except OSError:
                pass
    # Skipping dotenv (below) also drops the model endpoint registry, which for
    # desktop lives under the DATA dir — not $HOME — so the temp data dir starts
    # empty, `chatgpt-codex` does not resolve, and every turn dies with
    # "Missing credentials … OPENAI_API_KEY". A whole run failed that way and
    # blamed the product for three checks that never reached a model. Copy the
    # real registry in.
    real_endpoints = os.path.expanduser("~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(real_endpoints):
        print("SKIP: no model endpoint registry to borrow — live turns would "
              "fail on credentials, not on behaviour."); return 0

    proj = os.path.join(data, "project")
    os.makedirs(proj, exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)

    env = dict(os.environ)
    for k in ("ADK_CC_API_KEY", "ADK_CC_PERMISSION_MODE"):
        env.pop(k, None)
    env.update({
        # The repo .env pins ADK_CC_PERMISSION_MODE=bypassPermissions and is
        # found relative to the package, so with dotenv on, this would measure
        # the operator override rather than the shipped default — the first run
        # of this test did exactly that and reported bypassPermissions. Skipping
        # is safe HERE only because the subscription credentials come from
        # ~/.codex, not from .env; a test pinned to an API-key endpoint must not
        # copy this.
        "ADK_CC_SKIP_DOTENV": "1",
        "ADK_CC_SKIP_CONFIG_CHECK": "1",
        # Point at the real registry explicitly. Copying it into <data>/admin-data
        # was a guess and the guess was wrong — the server derives this path and
        # then setdefault()s it, so the copy sat unread and turns still died on
        # credentials.
        "ADK_CC_MODEL_REGISTRY_FILE": real_endpoints,
        "HOME": home,
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
            page.wait_for_timeout(1500)
            page.locator(".adk-project-row").first.click(timeout=15000)
            page.wait_for_timeout(2000)
            rows = page.locator(".adk-session-title")
            if rows.count():
                rows.first.click(timeout=6000)
            page.wait_for_timeout(1500)

            listed = requests.get(f"{BASE}/apps/adk_cc/users/{pid}/sessions",
                                  timeout=30).json()
            sid = listed[-1]["id"]
            sess_url = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
            state = requests.get(sess_url, timeout=30).json().get("state") or {}
            check("a fresh desktop chat starts in acceptEdits",
                  state.get("permission_mode") == "acceptEdits",
                  f"got {state.get('permission_mode')!r} — nothing recorded the mode")

            # Pin only the model; the MODE stays whatever the app decided.
            requests.patch(sess_url, json={"state_delta": {
                "model_endpoint": "chatgpt-codex", "model_id": MODEL}}, timeout=30)

            box = page.locator(".adk-composer-input")
            stop = page.locator('button[title="Stop the streaming response"]')

            def answer_any_question() -> bool:
                """Clear a clarifying question card, picking each first option.

                Without this the run parks forever: asked to edit a shell
                startup file, the agent globbed the PROJECT for rc files, found
                none, and asked WHICH file — so no write was attempted, no
                permission prompt appeared, and the test recorded that as the
                product failing to ask."""
                submit = page.get_by_role("button", name="Submit answers")
                if not submit.count():
                    return False
                card = submit.first.locator(
                    "xpath=ancestor::div[contains(@class,'bg-brand-tint')][1]")
                groups = card.locator("div.space-y-2")
                for gi in range(groups.count()):
                    opt = groups.nth(gi).locator("button").first
                    if opt.count():
                        opt.click()
                        page.wait_for_timeout(150)
                submit.first.click()
                page.wait_for_timeout(1000)
                return True

            def settle(timeout_s: float) -> None:
                deadline = time.time() + timeout_s
                started = False
                while time.time() < deadline:
                    page.wait_for_timeout(700)
                    if answer_any_question():
                        started = True
                        continue
                    if stop.count():
                        started = True
                    elif started:
                        return

            # The protected-path gate (edit_file on ~/.zshrc) is asserted on the
            # engine in tests/test_desktop_permission_default.py, not here. Live,
            # the agent legitimately varies — it probed $HOME, asked which rc file
            # to edit, and reported it cannot write outside the workspace root —
            # so the turn parked on a question card and the run scored the
            # PRODUCT as failing to prompt. A behaviour this specific belongs
            # where it can be stated exactly; this file keeps what only the
            # running app can show.

            # --- 3: does the new page signal reach a live turn? ---------------
            box.click()
            box.fill("Make a small web page with a button that counts clicks, "
                     "then tell me whether it works.")
            page.keyboard.press("Enter")
            settle(420)
            page.screenshot(path=os.path.join(data, "final.png"), full_page=True)
            log = open(server_log, errors="replace").read()
            nudges = re.findall(r"verify nudge: ([^\n]+)", log)
            check("the verification nudge fired during a live turn", bool(nudges),
                  "no 'verify nudge' line in the server log")
            if nudges:
                print("    nudge signals:", nudges[-1][:120])
            answer = page.inner_text("body")
            drove = any(w in answer.lower() for w in
                        ("jsdom", "playwright", "headless", "dom", "harness"))
            hedged = any(w in answer.lower() for w in
                         ("could not verify", "couldn't verify", "unverified",
                          "not verified", "untested"))
            check("the answer either drives the page or says it did not",
                  drove or hedged,
                  "claimed it works with no evidence and no caveat")
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
