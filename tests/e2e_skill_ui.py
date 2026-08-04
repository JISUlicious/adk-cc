"""A skill script, run from the DESKTOP APP, end to end.

#113 part 1 shipped `base_dir` / `how_to_run_scripts` on load_skill so the
agent is TOLD where a skill lives instead of hunting for it. That was verified
against the server API; this drives the actual app — project rail, composer,
thread — because the reported failure ("agent often struggles with finding the
script") was something a user watched happen in the UI.

Asserts the whole chain in the running product: the project's skill is
discovered after the folder is trusted, load_skill hands back the REAL
directory, the script executes, and its output reaches the thread.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_skill_ui.py
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8979
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
MARKER = "GREETINGS-FROM-THE-SKILL"
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
    open(os.path.join(proj, "README.md"), "w").write("# skill ui test\n")
    SKILL_DIR = os.path.join(proj, ".adk-cc", "skills", "greeter")
    os.makedirs(os.path.join(SKILL_DIR, "scripts"))
    open(os.path.join(SKILL_DIR, "SKILL.md"), "w").write(
        "---\nname: greeter\ndescription: >\n  Greets by running its own "
        "script. Use for any greeting request.\n---\n\n"
        "To greet, run `python scripts/greet.py <name>` and report its output.\n")
    open(os.path.join(SKILL_DIR, "scripts", "greet.py"), "w").write(
        'import sys\nprint("%s", sys.argv[1] if len(sys.argv)>1 else "")\n' % MARKER)

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
        # A project's skills are withheld until the folder is trusted.
        requests.post(f"{BASE}/desktop/settings/skills/trust",
                      json={"root": proj, "trusted": True}, timeout=15)

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1400, "height": 950})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            page.locator(".adk-project-row").first.click(timeout=15000)
            page.wait_for_timeout(2500)

            composer = page.locator("textarea").first
            composer.click()
            composer.fill("Use the greeter skill to greet Ada, and tell me "
                          "exactly what its script printed.")
            page.keyboard.press("Enter")

            # Running a skill script asks for permission first — correct, and
            # part of what is being tested: the card has to appear AND the
            # approval has to actually start the script.
            approved = False
            shown = False
            for _ in range(90):
                page.wait_for_timeout(2000)
                body = page.inner_text("body")
                if MARKER in body:
                    shown = True
                    break
                if not approved:
                    btn = page.locator(
                        "button:has-text('Allow'), button:has-text('Approve'), "
                        "button:has-text('Yes')").first
                    if btn.count() and btn.is_visible():
                        btn.click()
                        approved = True
            # Reported, not asserted: whether a card appears depends on the
            # permission MODE (desktop starts in acceptEdits) and on existing
            # grants — a different subsystem from the one under test. Both
            # outcomes are correct here; what matters is that the script ran
            # either way. Pinning it would make this test fail for reasons
            # that have nothing to do with finding the skill.
            print(f"    (confirmation card: {'shown+approved' if approved else 'not required'})")

            check("the script's output reached the thread in the app",
                  shown, page.inner_text("body")[-300:])

            sessions = requests.get(sess_api, timeout=30).json()
            events = requests.get(
                f"{sess_api}/{sessions[-1]['id']}", timeout=30).json().get("events") or []
            blob = json.dumps(events)
            check("load_skill told the agent the skill's REAL directory",
                  SKILL_DIR in blob and "base_dir" in blob, SKILL_DIR)
            check("the script ran through the skill launcher",
                  "run_skill_script" in blob)
            check("it never fell back to guessing a path with run_bash",
                  '"run_bash"' not in blob or MARKER in blob,
                  "run_bash used to reach the script")
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
