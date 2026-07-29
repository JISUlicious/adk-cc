"""W6.5: is the analysis runtime's state legible in the UI?

The failure it addresses is a silent one — the first analysis in a project
spends 20-60s installing packages, and a failed provision surfaces only as a
tool error deep in a turn. So the assertions are about what the USER can see,
driven by the three states that matter: not built yet, provisioning, ready.

The provisioning state is forced with the same sentinel `ensure_env` writes,
because waiting for a real cold install would make this test a minute long for
one label.

Run: .venv/bin/python tests/e2e_analysis_env_chip.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8953
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

    data = tempfile.mkdtemp(prefix="envchip-")
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
        "ADK_CC_SESSION_DSN": "sqlite:///" + os.path.join(data, "s.db"),
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub",
    })
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
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=10).json()["project"]["id"]
        q = f"?project_id={pid}&session_id=s1"

        # The endpoint must NOT provision — that is the whole contract.
        st = requests.get(f"{BASE}/desktop/analysis-env{q}", timeout=30).json()
        check("cold workspace reports 'absent'", st["state"] == "absent", st)
        check("the status call did not build an env",
              not os.path.exists(os.path.join(proj, ".adk-cc", "analysis-env")),
              "a read-only status endpoint provisioned an environment")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.locator(".adk-project-row").first.click(timeout=8000)
            page.wait_for_timeout(3000)
            check("cold state is visible in the composer",
                  "analysis env not built yet" in page.inner_text("body"),
                  page.inner_text("body")[-200:])

            # provisioning — the SAME sentinel ensure_env writes. Imported,
            # not hardcoded: the first version of this test pinned the old path
            # and kept passing after the constant moved.
            import sys as _sys
            _sys.path.insert(0, os.path.join(REPO, "agents"))
            from adk_cc.sandbox.analysis_env import _BUSY_REL, _ENV_REL

            busy = pathlib.Path(proj, _BUSY_REL)
            busy.parent.mkdir(parents=True, exist_ok=True)
            busy.write_text("core")
            envdir = pathlib.Path(proj, _ENV_REL)
            for _ in range(15):
                page.wait_for_timeout(1000)
                if "preparing analysis env" in page.inner_text("body"):
                    break
            check("provisioning is announced, not silent",
                  "preparing analysis env" in page.inner_text("body"),
                  page.inner_text("body")[-200:])
            page.screenshot(path=os.path.join(data, "chip-provisioning.png"), full_page=True)

            # ready
            busy.unlink()
            (envdir / "bin").mkdir(parents=True, exist_ok=True)
            (envdir / "bin" / "python").touch()
            (envdir / ".adk-cc-tiers").write_text("core|py3.12|deadbeef")
            for _ in range(15):
                page.wait_for_timeout(1000)
                if "core" in page.inner_text("body"):
                    break
            check("ready state names the installed tier",
                  "core" in page.inner_text("body"), page.inner_text("body")[-200:])
            shot = os.path.join(data, "chip-ready.png")
            page.screenshot(path=shot, full_page=True)
            print(f"    screenshots: {data}")
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
