"""Two confirmation cards, answered one at a time, in the real UI.

This is the check the unit tests cannot make. #114 and #115 both shipped with
a green unit suite and both failed in the product, because the failure lives
in the seam between the broker, ADK's resume path, and what the UI still has
on screen after a turn ends. The report was behavioural — "allowing one makes
the other unclickable, faded out" — so the test has to be behavioural too.

Drives a real desktop server with a real model:

  1. a prompt that makes the model gate TWO skill scripts in one round
  2. both cards must appear
  3. click Allow on the first — the SECOND must stay clickable, which is the
     exact thing that regressed
  4. click Allow on the second
  5. BOTH scripts must actually have run (their markers appear), and the model
     must reply — a stalled batch shows up as an empty round with no reply

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_stacked_confirmations_ui.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
PORT = 8987
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
MARK_A = "ALPHA-RAN-7731"
MARK_B = "BETA-RAN-4402"
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _skill(proj: str, name: str, marker: str) -> None:
    d = Path(proj, ".adk-cc", "skills", name, "scripts")
    d.mkdir(parents=True, exist_ok=True)
    (d.parent / "SKILL.md").write_text(textwrap.dedent(f"""\
        ---
        name: {name}
        description: >
          Runs the {name} report. Use when asked to run the {name} report.
        ---

        To produce the report, run `scripts/report.py`.
        """))
    (d / "report.py").write_text(f'print("{marker}")\n')


def main() -> int:  # noqa: PLR0915
    dist = REPO / "web" / "dist-desktop"
    if not dist.is_dir():
        print("SKIP: desktop UI not built."); return 0
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: set ADK_CC_LIVE=1 (drives a real model)."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed."); return 0

    data = tempfile.mkdtemp(prefix="stackconf-data-")
    proj = tempfile.mkdtemp(prefix="stackconf-proj-")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=False)
    _skill(proj, "alpha", MARK_A)
    _skill(proj, "beta", MARK_B)

    env = dict(os.environ)
    env.update({
        "ADK_CC_MODEL_REGISTRY_FILE": os.path.expanduser(
            "~/.adk-cc-desktop/admin-data/model-endpoints.json"),
        "ADK_CC_AGENTS_DIR": str(REPO / "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": str(dist), "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_NOOP_ACK_HOST_EXEC": "1",
        "ADK_CC_SKILL_SCRIPTS_ACK_HOST_EXEC": "1",
        "ADK_CC_DEFAULT_MODEL": MODEL,
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
    })
    log = os.path.join(data, "server.log")
    proc = subprocess.Popen(
        [str(REPO / ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(REPO), env=env, stdout=open(log, "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(200):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        # Project skills stay withheld until the folder is trusted.
        requests.post(f"{BASE}/desktop/settings/skills/trust",
                      json={"root": proj, "trusted": True}, timeout=15)

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            page.locator(".adk-project-row").first.click(timeout=20000)
            page.wait_for_timeout(2500)

            composer = page.locator("textarea").first
            composer.click()
            composer.fill(
                "Run BOTH reports in one go: the alpha report and the beta "
                "report. Use run_skill_script for each, calling them together, "
                "then tell me exactly what each printed.")
            page.keyboard.press("Enter")

            # A card is the container that HOLDS an Allow button. Locating
            # by button alone is what made the first two runs lie: get_by_role
            # matches disabled buttons, so an index-based click silently
            # re-targeted the card just answered and reported a UI bug that was
            # not there. Identify each card by the skill named in its text.
            def cards():
                return page.locator(".bg-brand-tint").filter(
                    has=page.get_by_role("button", name="Allow once"))

            def card_of(skill):
                for i in range(cards().count()):
                    c = cards().nth(i)
                    if f"{skill}:" in c.inner_text():
                        return c
                return None

            def allow_state(skill):
                """(present, clickable) for one skill's card."""
                c = card_of(skill)
                if c is None:
                    return False, False
                btn = c.get_by_role("button", name="Allow once")
                return True, btn.count() > 0 and btn.is_enabled()

            for _ in range(120):
                page.wait_for_timeout(1000)
                if cards().count() >= 2:
                    break
            n_cards = cards().count()
            check("the model gated BOTH scripts (two cards)", n_cards >= 2,
                  f"{n_cards} card(s) — the model may have run them one at a "
                  f"time; see {log}")
            if n_cards < 2:
                b.close(); return 1

            first, second = "alpha", "beta"
            if not allow_state(first)[1]:
                first, second = second, first
            print(f"    answering {first} first, then {second}")

            card_of(first).get_by_role("button", name="Allow once").click()
            page.wait_for_timeout(10000)

            present, clickable = allow_state(second)
            check("the OTHER card is still on screen", present,
                  "it vanished when the first was answered")
            check("and it is still CLICKABLE (the reported regression)",
                  clickable, "the surviving card was disabled/faded")
            check("the answered card locked only ITSELF",
                  not allow_state(first)[1])

            if clickable:
                card_of(second).get_by_role("button", name="Allow once").click()

            # 5. Both scripts must really have run, and the model must reply.
            deadline = time.time() + 300
            body = ""
            while time.time() < deadline:
                page.wait_for_timeout(3000)
                body = page.inner_text("body")
                if MARK_A in body and MARK_B in body:
                    break
            check("the FIRST script actually ran", MARK_A in body, body[-400:])
            check("the SECOND script actually ran", MARK_B in body, body[-400:])
            check("no card is left awaiting an answer",
                  not any(allow_state(s)[1] for s in ("alpha", "beta")),
                  "a confirmation card is still clickable after both answers")
            check("the user is not left staring at a spinner",
                  "agent is working" not in body.lower(), body[-300:])
            b.close()

        server_log = open(log, encoding="utf-8", errors="replace").read()
        check("the broker parked the partial batch (not ran it)",
              "confirmation answer parked" in server_log,
              "expected the park log line; the gate may not have fired")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n{_passed} passed, {_failed} failed")
    if _failed:
        print(f"server log: {log}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
