"""When does the plan-mode frame appear — and disappear?

Reported: the frame around the input showed up only after ALL streaming
finished, so for the whole planning turn the composer looked ordinary. Cause:
the UI read `session.state.permission_mode`, and the session record is only
refetched once the turn ends. `enter_plan_mode` writes ctx.state, which ADK
emits as an event action mid-stream, so the information was already on the wire.

Both transitions are timed against the TURN, not a wall clock: the frame must
be up while the planning turn is still streaming, and gone while the turn that
follows approval is still streaming.

Two harness notes, both of which produced false results before:
  * Drive the turn by TYPING. A turn started over `/api/turns` runs fine on the
    server but the browser never renders it — every DOM assertion then reads an
    empty pane, and "no frame" scores as a pass.
  * Read the PLACEHOLDER, not body text. The plan banner is always in the DOM
    (it reserves the footer height) and merely `invisible` when off.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_plan_mode_timing_ui.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8975
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
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs a live model turn (ADK_CC_LIVE=1)."); return 0
    from playwright.sync_api import sync_playwright

    data = tempfile.mkdtemp(prefix="planui-")
    proj = os.path.join(data, "project")
    os.makedirs(proj, exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)
    open(os.path.join(proj, "cli.py"), "w").write("def main():\n    print('hi')\n")

    env = dict(os.environ)
    for k in ("ADK_CC_API_KEY", "ADK_CC_SKIP_DOTENV", "ADK_CC_SKIP_CONFIG_CHECK"):
        env.pop(k, None)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": dist, "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SESSION_DSN": "sqlite:///" + os.path.join(data, "s.db"),
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

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            page.locator(".adk-project-row").first.click(timeout=15000)
            page.wait_for_timeout(2000)
            rows = page.locator(".adk-session-title")
            if rows.count():
                rows.first.click(timeout=6000)
            page.wait_for_timeout(1500)

            # Pin the model on whatever session the shell opened for us.
            listed = requests.get(
                f"{BASE}/apps/adk_cc/users/{pid}/sessions", timeout=30).json()
            if len(listed) != 1:
                print(f"    expected one open session, saw {len(listed)}"); return 1
            sid = listed[0]["id"]
            requests.patch(
                f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}",
                json={"state_delta": {"model_endpoint": "chatgpt-codex",
                                      "model_id": "chatgpt-codex/gpt-5.4-mini"}},
                timeout=30)

            box = page.locator(".adk-composer-input")
            stop = page.locator('button[title="Stop the streaming response"]')

            def in_plan_mode() -> bool:
                return "Plan mode" in (box.get_attribute("placeholder") or "")

            def streaming() -> bool:
                return stop.count() > 0

            def send(text: str) -> None:
                box.click()
                box.fill(text)
                page.keyboard.press("Enter")

            def watch(want_plan: bool, timeout_s: float) -> tuple[bool, bool]:
                """Poll until the turn stops streaming. Returns
                (saw_wanted_state_while_streaming, ever_streamed)."""
                seen = streamed = False
                deadline = time.time() + timeout_s
                while time.time() < deadline:
                    page.wait_for_timeout(400)
                    live = streaming()
                    streamed = streamed or live
                    if live and in_plan_mode() == want_plan:
                        seen = True
                        break
                    if streamed and not live:
                        break
                return seen, streamed

            check("no plan frame before planning starts", not in_plan_mode())

            send("Enter plan mode and plan (do not implement) adding a "
                 "--verbose flag to cli.py.")
            seen_on, streamed = watch(want_plan=True, timeout_s=300)
            # Wait out the rest of the turn either way.
            deadline = time.time() + 300
            while streaming() and time.time() < deadline:
                page.wait_for_timeout(1000)
            page.wait_for_timeout(1500)
            page.screenshot(path=os.path.join(data, "plan-on.png"), full_page=True)

            sess = requests.get(f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}",
                                timeout=30).json()
            entered = any(
                pt.get("functionCall", {}).get("name") == "enter_plan_mode"
                for e in sess["events"]
                for pt in ((e.get("content") or {}).get("parts") or []))
            check("the turn streamed in the browser (precondition)", streamed,
                  "the composer never showed a stop button — nothing was driven")
            check("the turn entered plan mode (precondition)", entered,
                  "model never called enter_plan_mode — timing is untestable here")
            if entered:
                check("the plan frame appears DURING the turn, not after it", seen_on,
                      "frame only showed once streaming had finished")
                check("the frame is still there when the turn ends", in_plan_mode())

            # --- exit: approving must drop the frame, also mid-turn ----------
            # Vacuous unless the frame was actually up, and that vacuous pass
            # is exactly what hid the harness bugs noted in the docstring.
            approve = page.get_by_role("button", name="Approve", exact=True)
            if entered and in_plan_mode() and approve.count():
                approve.first.click()
                seen_off, streamed2 = watch(want_plan=False, timeout_s=180)
                deadline = time.time() + 180
                while streaming() and time.time() < deadline:
                    page.wait_for_timeout(1000)
                page.wait_for_timeout(1500)
                check("approving clears the frame without waiting for the turn",
                      seen_off if streamed2 else not in_plan_mode(),
                      "frame persisted while the post-approval turn streamed")
            else:
                print("    [skip] no Approve button — plan was never offered")
            page.screenshot(path=os.path.join(data, "plan-off.png"), full_page=True)
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
