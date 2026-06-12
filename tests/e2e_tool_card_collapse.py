"""E2E: tool-call cards collapse + bound long output (ACTUAL rendering).

Verifies the user-visible behavior in a real browser against the real
built bundle: a run_bash tool call with long output (a) renders COLLAPSED
by default (only the header shows — the terminal block is not in the DOM),
and (b) when expanded, the terminal output is HEIGHT-CAPPED and scrollable
(max-h-80 ≈ 320px) instead of dumping hundreds of lines into the thread.

Mounts the REAL BashTerminalCard by seeding a session whose event log
already contains a run_bash functionCall + functionResponse pair (ADK
persists seeded events), then selecting it and measuring — no model turn.

Run directly:

    .venv/bin/python tests/e2e_tool_card_collapse.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import requests
from playwright.sync_api import sync_playwright

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(REPO, "web")
PORT = 8773
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "tok"
USER = "alice"
APP = "adk_cc"

VIEWPORT = {"width": 1280, "height": 900}  # tall, so max-h-80 (320) bounds it
CALL_ID = "fc-bash-1"
CMD = "echo BASHPROBE"
# ~200 lines → far taller than the 320px cap, so the cap must engage.
LONG_STDOUT = "BASHPROBE-OUT\n" + "\n".join(f"line {i}" for i in range(200)) + "\n"


def _build() -> None:
    env = dict(os.environ)
    env["VITE_ADK_CC_HTML_PREVIEW_ALLOW_SCRIPTS"] = "1"
    subprocess.run(["npm", "run", "build"], cwd=WEB, env=env, check=True,
                   capture_output=True)


def _start_server(data_dir: str):
    env = dict(os.environ)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub",
        "ADK_CC_AUTH_TOKENS": f"{TOKEN}={USER}:local",
        "ADK_CC_SERVE_UI": "1", "ADK_CC_UI_DIST": os.path.join(WEB, "dist"),
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{data_dir}",
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"),
         "adk_cc.service.server:make_app", "--factory",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        try:
            requests.get(BASE + "/", timeout=1)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("server did not start")


def _seed_session() -> str:
    events = [
        {
            "id": "evt-call", "author": "agent", "invocationId": "inv1",
            "content": {"role": "model", "parts": [
                {"functionCall": {
                    "id": CALL_ID, "name": "run_bash",
                    "args": {"command": CMD, "timeout_seconds": 30},
                }},
            ]},
        },
        {
            "id": "evt-resp", "author": "agent", "invocationId": "inv1",
            "content": {"role": "user", "parts": [
                {"functionResponse": {
                    "id": CALL_ID, "name": "run_bash",
                    "response": {
                        "status": "ok", "command": CMD, "exit_code": 0,
                        "stdout": LONG_STDOUT, "stderr": "",
                    },
                }},
            ]},
        },
    ]
    r = requests.post(
        f"{BASE}/apps/{APP}/users/{USER}/sessions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"events": events}, timeout=10,
    )
    r.raise_for_status()
    return r.json()["id"]


# The terminal <pre> is identified by its dark inline background (#141413
# == rgb(20, 20, 19)); read its presence + box metrics.
_MEASURE = """
() => {
  const term = [...document.querySelectorAll('pre')].find(
    p => getComputedStyle(p).backgroundColor === 'rgb(20, 20, 19)');
  if (!term) return { present: false };
  return {
    present: true,
    clientH: term.clientHeight,    // visible box (incl padding, excl scroll)
    scrollH: term.scrollHeight,    // full content height
  };
}
"""


def main() -> int:
    backup = os.path.join(WEB, "dist.toolcard-backup")
    dist = os.path.join(WEB, "dist")
    if os.path.isdir(dist):
        if os.path.isdir(backup):
            shutil.rmtree(backup)
        shutil.move(dist, backup)
    data_dir = os.path.join(REPO, ".workspace", "toolcard-e2e")
    if os.path.isdir(data_dir):
        shutil.rmtree(data_dir)
    os.makedirs(data_dir, exist_ok=True)

    proc = None
    ok = True

    def check(name, cond, detail):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}: {detail}")
        ok = ok and cond

    try:
        print("building bundle…")
        _build()
        proc = _start_server(data_dir)
        sid = _seed_session()
        print(f"seeded session {sid[:18]} with a long run_bash call")

        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page(viewport=VIEWPORT)
            pg.goto(BASE + "/")
            pg.evaluate(
                "(t)=>{localStorage.setItem('adk_cc.token',t);"
                "localStorage.setItem('adk_cc.user','alice')}", TOKEN,
            )
            pg.reload()
            pg.wait_for_load_state("networkidle")
            pg.get_by_text(sid[:18]).first.click()
            # The bash card header (command text) must be present…
            pg.get_by_text(CMD).first.wait_for(timeout=10000)
            pg.wait_for_timeout(300)

            # 1) COLLAPSED by default — terminal block not rendered yet.
            before = pg.evaluate(_MEASURE)
            check("run_bash card is collapsed by default",
                  before.get("present") is False,
                  f"terminal block present={before.get('present')} (want False)")

            # 2) Expand → terminal block appears, height-capped + scrollable.
            pg.get_by_text(CMD).first.click()
            pg.wait_for_timeout(400)
            after = pg.evaluate(_MEASURE)
            b.close()

        if not after.get("present"):
            check("terminal renders after expand", False, "still absent")
            print("\ntool-card-collapse e2e FAILED")
            return 1

        ch, sh = after["clientH"], after["scrollH"]
        # max-h-80 = 320px (border-box) → clientHeight ≤ 320 (+tiny tolerance).
        check("expanded output is height-capped (≤320px)",
              ch <= 322, f"clientHeight={ch}px (cap 320)")
        # content far exceeds the box → it's truncated to a scroll region.
        check("long output is scrollable (not dumped inline)",
              sh > ch + 100, f"scrollHeight={sh} > clientHeight={ch}")

        print("\ntool-card-collapse e2e " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        if proc:
            proc.kill()
        if os.path.isdir(backup):
            if os.path.isdir(dist):
                shutil.rmtree(dist)
            shutil.move(backup, dist)


if __name__ == "__main__":
    sys.exit(main())
