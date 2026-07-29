"""W6.6: can a user find out what the agent can do, without asking it?

23 skills ship with the app and the only way to learn they existed was the
model calling `list_skills` mid-turn. `/skills` opens the catalog; the catalog
shows what each skill is FOR, not just its name.

Run: .venv/bin/python tests/e2e_skill_discovery_ui.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8957
BASE = f"http://127.0.0.1:{PORT}"
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
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    data = tempfile.mkdtemp(prefix="skilldisc-")
    proj = os.path.join(data, "project")
    os.makedirs(proj, exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": dist, "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_TENANT_SKILLS_DIR": os.path.join(data, "skills"),
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub",
    })
    # a skill the USER installed, so both sections are exercised
    mine = os.path.join(data, "skills", "local", "my-own-skill")
    os.makedirs(mine, exist_ok=True)
    with open(os.path.join(mine, "SKILL.md"), "w") as f:
        f.write("---\nname: my-own-skill\ndescription: A skill this user added themselves.\n---\n\nBody.\n")

    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        requests.post(BASE + "/desktop/projects", json={"path": proj}, timeout=10)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.locator(".adk-project-row").first.click(timeout=8000)
            page.wait_for_timeout(2500)

            box = page.locator("textarea, [contenteditable=true]").first
            box.click()
            box.type("/skills")
            page.wait_for_timeout(600)
            menu = page.inner_text("body")
            check("/skills is offered in the command menu", "skills" in menu.lower(),
                  menu[-200:])
            page.keyboard.press("Enter")
            page.wait_for_timeout(2500)

            text = page.inner_text("body")
            check("it opens the skill catalog", "Skills" in text, text[:200])
            # the built-ins that ship with the app
            for name in ("data-analyst", "incident-postmortem", "sql-queries"):
                check(f"'{name}' is listed", name in text, "")
            check("descriptions are shown, not just names",
                  "postmortem" in text.lower() and
                  any(w in text.lower() for w in ("timeline", "evidence", "blameless")),
                  "no description text found")
            n = page.locator("[data-skill]").count()
            check("the whole catalog is listed", n >= 20, f"{n} skills")

            # Grouping: a user's own skills must not be buried under 23 built-ins.
            check("the two groups are labelled",
                  "Installed here" in text and "Built in" in text, text[:300])
            check("built-ins say they cannot be removed",
                  "not removable" in text, text[:300])
            order_mine = text.find("Installed here")
            order_builtin = text.find("Built in")
            check("your own skills come first", 0 <= order_mine < order_builtin,
                  f"mine@{order_mine} builtin@{order_builtin}")
            own = page.locator('[data-skill="my-own-skill"]')
            check("the user-installed skill is in the first group",
                  own.count() == 1 and own.first.evaluate(
                      "el => el.closest('section').querySelector('h4').textContent"
                  ).startswith("Installed here"),
                  "own skill not grouped under Installed here")
            page.screenshot(path=os.path.join(data, "skills.png"), full_page=True)
            print(f"    screenshot: {os.path.join(data, 'skills.png')}")
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
