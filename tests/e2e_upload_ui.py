"""Live UI e2e (#121 P1): attach a file in the composer, the model reads it.

Full pipeline, real everything: Playwright stages a CSV through the
composer's file input, sends a message, the upload lands at uploads/<name>
in the project workspace, the message carries the attachment line, and a
REAL model reads the file with its fs tools and answers with the value
inside. Skips cleanly without a model endpoint / UI build / playwright.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_upload_ui.py
"""

from __future__ import annotations

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

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + str(detail)) if detail and not ok else ''}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0
    endpoints = os.path.expanduser(
        "~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(endpoints):
        print("SKIP: no model endpoint registry to borrow."); return 0
    dist = os.path.join(REPO, "web", "dist-desktop")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print("SKIP: web UI not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    data = tempfile.mkdtemp(prefix="uplui-")
    proj = tempfile.mkdtemp(prefix="uplui-proj-")
    subprocess.run(["git", "init", "-q", proj], capture_output=True)

    FNAME = "매출 보고서 (1).csv"  # Hangul + space + parens: the widened name rule, end to end
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
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SERVE_UI": "1", "ADK_CC_UI_DIST": dist,
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

        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        sid = "s-upload"
        sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
        requests.post(sess, json={}, timeout=30)
        requests.patch(sess, json={"state_delta": {
            "model_endpoint": "chatgpt-codex", "model_id": MODEL}}, timeout=30)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            proj_row = page.get_by_text(os.path.basename(proj), exact=False).first
            if proj_row.count() > 0:
                proj_row.click()
                page.wait_for_timeout(1200)
            row = page.locator(".adk-session-row").first
            if row.count() > 0:
                row.click()
            page.wait_for_timeout(2000)

            # Stage the file through the hidden input, assert the chip.
            page.set_input_files("input[type=file]", csv_path)
            page.wait_for_timeout(400)
            chip = page.locator(f'[data-upload-chip="{FNAME}"]')
            check("staged chip appears", chip.count() > 0)

            box = page.locator("textarea.adk-composer-input")
            box.fill("Read the attached file and tell me the magic word "
                     "in it, exactly.")
            page.screenshot(path=os.path.join(data, "staged.png"))
            page.get_by_title("Send (Enter)").click()

            # The upload happens before the send; the file must land fast.
            dest = os.path.join(proj, "uploads", FNAME)
            landed = False
            for _ in range(40):
                if os.path.isfile(dest):
                    landed = True
                    break
                page.wait_for_timeout(500)
            check(f"file lands at uploads/{FNAME} in the project", landed)
            if landed:
                check("bytes exact (sha256)",
                      hashlib.sha256(open(dest, "rb").read()).hexdigest()
                      == hashlib.sha256(csv_body.encode()).hexdigest())
            check("chips cleared after send",
                  page.locator("[data-upload-chip]").count() == 0)

            # The model reads it with its own tools and answers.
            answered = ""
            for _ in range(120):
                page.wait_for_timeout(3000)
                evs = requests.get(sess, timeout=30).json().get("events", [])
                texts = [p.get("text") or ""
                         for e in evs
                         if (e.get("author") or "") != "user"
                         for p in ((e.get("content") or {}).get("parts") or [])]
                if any(MAGIC in t for t in texts):
                    answered = "found"
                    break
                # stop early if the turn errored
                if any("error" in (e.get("errorMessage") or "").lower()
                       for e in evs):
                    break
            check("the model read the uploaded file and named the magic word",
                  answered == "found")

            # The user message carries the attachment line (model-visible
            # contract, and what the thread shows).
            evs = requests.get(sess, timeout=30).json().get("events", [])
            user_texts = [p.get("text") or ""
                          for e in evs if (e.get("author") or "") == "user"
                          for p in ((e.get("content") or {}).get("parts") or [])]
            check("message carries the attachment line",
                  any(f"[attached file: uploads/{FNAME}" in t
                      for t in user_texts),
                  [t[:80] for t in user_texts][:4])

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
