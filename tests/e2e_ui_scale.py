"""The text-size selector, in the built desktop bundle.

Checks the three things that make it a real setting rather than a control that
merely moves: it changes the rendered size, it survives a reload, and it scales
SPACING with the type (the reason it drives the root font size instead of
overriding text classes — otherwise text grows inside boxes that stayed put).

No model needed.

Run: .venv/bin/python tests/e2e_ui_scale.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8967
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
    except ImportError:
        print("SKIP: playwright not installed."); return 0

    data = tempfile.mkdtemp(prefix="uiscale-")
    proj = os.path.join(data, "project")
    os.makedirs(proj, exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)

    env = dict(os.environ)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_API_KEY": "sk-dummy-for-tests",
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": dist, "ADK_CC_SANDBOX_BACKEND": "noop",
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        requests.post(BASE + "/desktop/projects", json={"path": proj}, timeout=15)

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1200)

            def root_px() -> float:
                return page.evaluate(
                    "parseFloat(getComputedStyle(document.documentElement).fontSize)")

            def composer_height() -> float:
                el = page.locator(".adk-composer-input")
                box = el.bounding_box() if el.count() else None
                return box["height"] if box else 0.0

            base_px, base_h = root_px(), composer_height()
            check("starts at the browser default", abs(base_px - 16) < 0.6,
                  f"root font-size is {base_px}px")

            page.locator("text=Settings").first.click()
            page.wait_for_timeout(800)
            appearance = page.get_by_role("button", name="Appearance")
            if appearance.count():
                appearance.first.click()
                page.wait_for_timeout(500)
            large = page.get_by_role("button", name="Large")
            check("the control is reachable in Appearance", large.count() > 0,
                  "no Large option found in the settings modal")
            if not large.count():
                page.screenshot(path=os.path.join(data, "no-control.png"), full_page=True)
                print(f"    artifacts: {data}")
                return 1
            large.first.click()
            page.wait_for_timeout(600)
            big_px = root_px()
            check("choosing Large scales the interface up", big_px > base_px + 2,
                  f"{base_px}px → {big_px}px")

            # Close the modal and compare a real control's height: if only the
            # font moved, the composer would be the same size with bigger text
            # spilling inside it.
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            big_h = composer_height()
            check("spacing scales with the type, not just the glyphs",
                  big_h > base_h + 1 if base_h else True,
                  f"composer height {base_h}px → {big_h}px")

            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1000)
            check("the choice survives a reload", abs(root_px() - big_px) < 0.6,
                  f"after reload root font-size is {root_px()}px, expected {big_px}px")

            page.screenshot(path=os.path.join(data, "large.png"), full_page=True)
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
