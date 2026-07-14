"""E2E: the desktop Sandbox settings section (real browser).

Boots a desktop-mode server, opens Settings → Sandbox, asserts the runtime is
detected, flips the Container-sandbox toggle on, and confirms it persisted
server-side (GET /desktop/settings/sandbox → mode=container) and that the
network + image rows appear. SKIPS if no container runtime is detected.

Model-free. Run: SHOT_DIR=/tmp .venv/bin/python tests/e2e_sandbox_ui.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOT_DIR = os.environ.get("SHOT_DIR", "/tmp")
PORT = 8941
BASE = f"http://127.0.0.1:{PORT}"

_passed = _failed = 0


def check(name: str, ok: bool) -> None:
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    # Skip cleanly when no runtime — the section only lights up with Docker/Podman.
    try:
        sys.path.insert(0, os.path.join(REPO, "agents"))
        from adk_cc.sandbox.backends.container_runtime import detect_runtime
        if detect_runtime() is None:
            print("[SKIP] no local container runtime detected")
            return 0
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] detection import failed: {e}")
        return 0

    from playwright.sync_api import sync_playwright

    data = tempfile.mkdtemp(prefix="sbx-ui-")
    key = subprocess.run(
        [os.path.join(REPO, ".venv/bin/python"), "-c",
         "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"],
        capture_output=True, text=True).stdout.strip()
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data,
        "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local",
        "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": os.path.join(REPO, "web/dist-desktop"),
        "ADK_CC_SANDBOX_BACKEND": "noop",  # don't force container; the UI drives the setting
        "ADK_CC_SESSION_DSN": "sqlite:///" + os.path.join(data, "sessions.db"),
        "ADK_CC_CREDENTIAL_PROVIDER": "encrypted_file",
        "ADK_CC_CREDENTIAL_KEY": key,
        "ADK_CC_CREDENTIAL_STORE_DIR": os.path.join(data, "secrets"),
        "ADK_CC_SKIP_DOTENV": "1",
        "ADK_CC_API_KEY": "stub",
    })
    # ensure no env override of the mode so the UI toggle is what drives it
    env.pop("ADK_CC_SANDBOX_MODE", None)
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

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 860})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector('button[title="Settings"]', timeout=20000)
            page.click('button[title="Settings"]')
            page.get_by_role("button", name="Sandbox").click()
            page.wait_for_selector("text=Container sandbox", timeout=10000)
            check("Sandbox tab shows the runtime as detected",
                  page.get_by_text("detected").count() > 0)
            page.screenshot(path=f"{SHOT_DIR}/sandbox_settings.png")

            # flip the mode switch (first role=switch in the section) on
            sw = page.get_by_role("switch").first
            check("mode is host before toggling",
                  requests.get(BASE + "/desktop/settings/sandbox", timeout=5).json()["mode"] == "host")
            sw.click()
            page.wait_for_selector("text=Allow network", timeout=10000)
            check("toggling on reveals the network + image controls",
                  page.get_by_text("Allow network").count() > 0
                  and page.get_by_text("Image").count() > 0)
            check("mode persisted to container server-side",
                  requests.get(BASE + "/desktop/settings/sandbox", timeout=5).json()["mode"] == "container")
            page.screenshot(path=f"{SHOT_DIR}/sandbox_settings_on.png")

            # composer indicator (item 2): with container mode on, a fresh chat
            # view shows the "Sandboxed" badge (the badge fetches on mount).
            page.keyboard.press("Escape")  # close settings
            page.reload(wait_until="networkidle")
            page.wait_for_selector("text=Sandboxed", timeout=10000)
            check("composer shows the Sandboxed badge when container mode is on",
                  page.get_by_text("Sandboxed").count() > 0)
            page.screenshot(path=f"{SHOT_DIR}/sandbox_badge.png")

            # flip back off → the badge disappears
            requests.put(BASE + "/desktop/settings/sandbox", json={"mode": "host"}, timeout=5)
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1200)
            check("badge disappears in host mode", page.get_by_text("Sandboxed").count() == 0)
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
