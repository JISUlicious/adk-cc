"""Live UI e2e (#121 P1): attach a file in the composer, the model reads it.

Full pipeline, real everything: Playwright stages a CSV through the
composer's file input, sends a message, the upload lands at uploads/<name>
in the workspace, the message carries the attachment line, and a REAL model
reads the file with its fs tools and answers with the value inside.

BOTH shells (they hit different routes — /desktop/uploads vs /api/uploads —
and different workspace resolution):

  desktop:        ADK_CC_LIVE=1 .venv/bin/python tests/e2e_upload_ui.py
  web:      WEB=1 ADK_CC_LIVE=1 .venv/bin/python tests/e2e_upload_ui.py

The filename deliberately exercises the widened name rule (Hangul + space +
parentheses) end to end. Skips cleanly without a model endpoint / UI build /
playwright.
"""

from __future__ import annotations

import glob
import hashlib
import os
import subprocess
import tempfile
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8944
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
MAGIC = "xylophone-42"
FNAME = "매출 보고서 (1).csv"  # Hangul + space + parens

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + str(detail)) if detail and not ok else ''}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:  # noqa: PLR0915
    web_shell = os.environ.get("WEB") == "1"
    shell = "web" if web_shell else "desktop"
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0
    endpoints = os.path.expanduser(
        "~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(endpoints):
        print("SKIP: no model endpoint registry to borrow."); return 0
    dist = os.path.join(REPO, "web", "dist" if web_shell else "dist-desktop")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print(f"SKIP: {shell} UI not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    data = tempfile.mkdtemp(prefix=f"uplui-{shell}-")
    proj = tempfile.mkdtemp(prefix="uplui-proj-")
    subprocess.run(["git", "init", "-q", proj], capture_output=True)
    wsroot = tempfile.mkdtemp(prefix="uplui-wsroot-")

    csv_path = os.path.join(data, FNAME)
    csv_body = f"key,value\nmagic_word,{MAGIC}\n"
    with open(csv_path, "w") as fh:
        fh.write(csv_body)

    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    for k in list(env):
        if k.startswith("ADK_CC_UPLOAD"):
            env.pop(k)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_MODEL_REGISTRY_FILE": endpoints,
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        # ADK_CC_DATA_DIR, not just the desktop alias — a web run without it
        # writes into the operator's REAL ~/.adk-cc (the 25-session lesson).
        "ADK_CC_DATA_DIR": data,
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SERVE_UI": "1", "ADK_CC_UI_DIST": dist,
        "ADK_CC_DEFAULT_MODEL": MODEL,
    })
    if web_shell:
        env["ADK_CC_WORKSPACE_ROOT"] = wsroot
        env["ADK_CC_NOOP_ACK_HOST_EXEC"] = "1"
    else:
        env["ADK_CC_DESKTOP"] = "1"

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

        if not web_shell:
            pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                                timeout=15).json()["project"]["id"]
            sid = "s-upload"
            sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
            requests.post(sess, json={}, timeout=30)
            requests.patch(sess, json={"state_delta": {
                "model_endpoint": "chatgpt-codex", "model_id": MODEL}}, timeout=30)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 950})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            if web_shell:
                # The web shell opens session-less with a disabled composer.
                page.get_by_role("button", name="New").first.click(timeout=20000)
            else:
                proj_row = page.get_by_text(os.path.basename(proj),
                                            exact=False).first
                if proj_row.count() > 0:
                    proj_row.click()
                    page.wait_for_timeout(1200)
                row = page.locator(".adk-session-row").first
                if row.count() > 0:
                    row.click()
            page.wait_for_timeout(2500)

            # Stage the file through the hidden input, assert the chip.
            page.set_input_files("input[type=file]", csv_path)
            page.wait_for_timeout(400)
            chip = page.locator(f'[data-upload-chip="{FNAME}"]')
            check(f"{shell}: staged chip appears", chip.count() > 0)

            box = page.locator("textarea.adk-composer-input")
            box.fill("Read the attached file and tell me the magic word "
                     "in it, exactly.")
            page.screenshot(path=os.path.join(data, "staged.png"))
            page.get_by_title("Send (Enter)").click()

            # The upload happens before the send; the file must land fast.
            def _dest():
                if web_shell:
                    hits = glob.glob(os.path.join(
                        wsroot, "*", "*", "uploads", FNAME))
                    return hits[0] if hits else None
                d = os.path.join(proj, "uploads", FNAME)
                return d if os.path.isfile(d) else None

            dest = None
            for _ in range(40):
                dest = _dest()
                if dest:
                    break
                page.wait_for_timeout(500)
            check(f"{shell}: file lands at uploads/{FNAME}", bool(dest),
                  f"searched {'wsroot' if web_shell else 'project'}")
            if dest:
                check(f"{shell}: bytes exact (sha256)",
                      hashlib.sha256(open(dest, "rb").read()).hexdigest()
                      == hashlib.sha256(csv_body.encode()).hexdigest())
            check(f"{shell}: chips cleared after send",
                  page.locator("[data-upload-chip]").count() == 0)

            # The model reads it with its own tools and answers. DOM-based:
            # works identically in both shells (the web shell's user id is
            # UI-internal, so no REST session URL to poll).
            answered = False
            for _ in range(120):
                page.wait_for_timeout(3000)
                if page.get_by_text(MAGIC, exact=False).count() > 0:
                    answered = True
                    break
            check(f"{shell}: the model read the file and named the magic word",
                  answered)
            check(f"{shell}: thread shows the attachment line",
                  page.get_by_text(f"[attached file: uploads/{FNAME}",
                                   exact=False).count() > 0)

            page.screenshot(path=os.path.join(data, "answered.png"))
            browser.close()
        print(f"    screenshots: {data}/staged.png, {data}/answered.png")
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
