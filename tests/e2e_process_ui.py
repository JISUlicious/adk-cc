"""Live UI e2e + demo (#131): a foreground script in the process dock.

Real model turn asks the agent to run a slow ticking script through its
code executor; Playwright then proves — and screenshots — the whole arc:
the script row appears in the dock WHILE running (badge + elapsed/budget),
the log drawer tails the ticks LIVE, Stop kills it mid-run, and the turn
still completes with the partial output.

  ADK_CC_LIVE=1 .venv/bin/python tests/e2e_process_ui.py

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
PORT = 8975
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
OUT = os.path.expanduser("~/adk-cc-process-ui-demo")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + str(detail)[:120]) if detail and not ok else ''}",
          flush=True)
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:  # noqa: PLR0915
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns."); return 0
    endpoints = os.path.expanduser(
        "~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(endpoints):
        print("SKIP: no model endpoint registry."); return 0
    dist = os.path.join(REPO, "web", "dist")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print("SKIP: web UI not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    os.makedirs(OUT, exist_ok=True)
    data = tempfile.mkdtemp(prefix="procui-")
    wsroot = tempfile.mkdtemp(prefix="procui-ws-")
    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_MODEL_REGISTRY_FILE": endpoints,
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_DATA_DIR": data, "ADK_CC_DESKTOP_DATA": data,
        "ADK_CC_TENANCY_MODE": "single", "ADK_CC_GLOBAL_TENANT_ID": "local",
        "ADK_CC_SANDBOX_BACKEND": "noop", "ADK_CC_NOOP_ACK_HOST_EXEC": "1",
        "ADK_CC_SERVE_UI": "1", "ADK_CC_UI_DIST": dist,
        "ADK_CC_DEFAULT_MODEL": MODEL,
        "ADK_CC_WORKSPACE_ROOT": wsroot,
        # The seeded demo skill lives in the session workspace; the #116
        # trust gate would withhold it (correctly) without this.
        "ADK_CC_TRUST_PROJECT_SKILLS": "1",
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"),
         "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env,
        stdout=open(os.path.join(data, "server.log"), "w"),
        stderr=subprocess.STDOUT)
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
            sid = requests.get(
                f"{BASE}/apps/adk_cc/users/alice/sessions",
                timeout=10).json()[0]["id"]
            requests.patch(
                f"{BASE}/apps/adk_cc/users/alice/sessions/{sid}",
                json={"state_delta": {"permission_mode": "bypassPermissions"}},
                timeout=15)

            # Seed a PROJECT skill with a slow ticking script — the code
            # executor only serves skill scripts (there is no generic
            # run_code tool), so this is the real path users hit.
            ws = os.path.join(wsroot, "local", "alice", ".sessions", sid)
            sk = os.path.join(ws, ".adk-cc", "skills", "slow-probe")
            os.makedirs(os.path.join(sk, "scripts"), exist_ok=True)
            with open(os.path.join(sk, "SKILL.md"), "w") as fh:
                fh.write("---\nname: slow-probe\ndescription: >\n"
                         "  Long-running diagnostic probe (demo).\n---\n\n"
                         "Run scripts/slow.py with run_skill_script.\n")
            with open(os.path.join(sk, "scripts", "slow.py"), "w") as fh:
                fh.write("import time\n"
                         "for i in range(240):\n"
                         "    print(f'tick {i}', flush=True)\n"
                         "    time.sleep(1)\n"
                         "print('DONE')\n")

            box = page.locator("textarea.adk-composer-input")
            box.fill(
                "Use run_skill_script to run the 'slow-probe' skill's "
                "scripts/slow.py (no arguments), then tell me its last "
                "output line.")
            page.get_by_title("Send (Enter)").click()

            # The #114 skill-script gate asks even under bypass (third-party
            # code is the danger floor) — approve it like a user would.
            import re as _re

            approved = False
            for _ in range(90):
                page.wait_for_timeout(1000)
                # The gate offers Allow once / Allow always / Deny (bash-gate
                # shape); older cards say just Allow. Never match Deny.
                allow = page.get_by_role(
                    "button", name=_re.compile(r"^Allow( once)?$"))
                if allow.count() > 0:
                    allow.first.click()
                    approved = True
                    break
            check("skill-gate confirmation approved", approved)

            # ---- the row appears WHILE the script runs -------------------
            row_seen = badge_seen = budget_seen = False
            for _ in range(90):
                page.wait_for_timeout(1000)
                dock = page.locator("[data-process-dock]")
                if dock.count() == 0:
                    continue
                row_seen = True
                if page.get_by_text("script", exact=True).count() > 0:
                    badge_seen = True
                if page.get_by_text("/ 5m", exact=False).count() > 0:
                    budget_seen = True
                if badge_seen and budget_seen:
                    break
            check("dock shows the running script row", row_seen)
            check("row carries the 'script' badge", badge_seen)
            check("row shows elapsed / budget", budget_seen)
            page.screenshot(path=os.path.join(OUT, "1-running.png"))

            # ---- live tail in the drawer ---------------------------------
            # The FIRST exec is the materialisation probe (it also builds
            # the analysis venv, cold) — the script itself is a SECOND
            # record. Find the running record whose log already ticks, then
            # open ITS drawer.
            tick_rec = None
            for _ in range(180):
                time.sleep(1)
                try:
                    rows_ = requests.get(
                        f"{BASE}/api/processes?project_id=alice",
                        timeout=5).json()["processes"]
                    for r_ in rows_:
                        if r_["status"] != "running":
                            continue
                        lg = requests.get(
                            f"{BASE}/api/processes/{r_['id']}/log",
                            timeout=5).json().get("log", "")
                        if "tick" in lg:
                            tick_rec = r_["id"]
                            break
                except Exception:
                    pass
                if tick_rec:
                    break
            check("a running record's log carries live ticks (API)",
                  bool(tick_rec))
            tick_seen = False
            if tick_rec:
                page.locator(f'[data-process="{tick_rec}"]') \
                    .get_by_role("button").first.click()
                for _ in range(15):
                    page.wait_for_timeout(1000)
                    if page.locator("[data-process-log]").get_by_text(
                            "tick", exact=False).count() > 0:
                        tick_seen = True
                        break
            check("log drawer tails the ticks LIVE", tick_seen)
            page.screenshot(path=os.path.join(OUT, "2-live-log.png"))
            close = page.get_by_label("Close log")
            if close.count() > 0:
                close.first.click()  # the drawer overlays the Stop button
            page.wait_for_timeout(500)

            # ---- Stop mid-run --------------------------------------------
            stop = page.get_by_title("Stop this process")
            check("Stop control offered (noop backend)", stop.count() > 0)
            if stop.count() > 0:
                stop.first.click()
            stopped = False
            for _ in range(20):
                page.wait_for_timeout(1000)
                if page.get_by_text("stopped", exact=False).count() > 0:
                    stopped = True
                    break
            check("row flips to 'stopped'", stopped)
            page.screenshot(path=os.path.join(OUT, "3-stopped.png"))

            # Finished foreground rows AUTO-CLEAR after a short grace (user
            # report: the panel silted up with 'exited' rows).
            cleared = False
            for _ in range(15):
                page.wait_for_timeout(1000)
                if page.get_by_text("stopped", exact=False).count() == 0:
                    cleared = True
                    break
            check("stopped row auto-clears after the grace period", cleared)
            page.screenshot(path=os.path.join(OUT, "3b-autocleared.png"))

            # ---- the TURN survives the Stop ------------------------------
            replied = False
            for _ in range(120):
                page.wait_for_timeout(2000)
                if page.get_by_text("tick", exact=False).count() > 1:
                    # the model's reply quotes the partial output
                    replied = True
                    break
                if page.locator("textarea.adk-composer-input").is_enabled() \
                        and page.get_by_title("Send (Enter)").is_enabled():
                    replied = page.get_by_text("DONE", exact=False).count() == 0
                    break
            check("turn completes after Stop (partial output, no DONE)",
                  replied)
            page.screenshot(path=os.path.join(OUT, "4-turn-survives.png"))
            browser.close()
        print(f"screenshots: {OUT}")
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
