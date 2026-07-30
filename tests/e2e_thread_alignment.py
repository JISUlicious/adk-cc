"""Every row in the thread ends at the same x.

Reported: tool-call messages, the aggregated tool-call bubble and model message
bubbles all had different widths. They did — four different rules across a
dozen components: seven cards at `max-w-[80%] w-full`, three at plain `w-full`
(so 100%), the aggregated-outputs card with no row wrapper at all, and model
text with a max-width but no `w-full`, so it hugged its content and ended
somewhere new on every message.

Measured, not eyeballed: this seeds a session with one of each row type, then
compares the right edge of every left-aligned row. A screenshot would show the
ragged edge; only geometry proves it is gone.

Run: .venv/bin/python tests/e2e_thread_alignment.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "agents"))
PORT = 8965
BASE = f"http://127.0.0.1:{PORT}"
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _seed(data_dir: str, project_id: str, session_id: str) -> None:
    """Write one row of every kind straight into the session store."""
    import asyncio

    from google.adk.events.event import Event
    from google.genai import types

    from adk_cc.service.file_session_service import FileSessionService

    def model(*parts):
        return Event(author="coordinator",
                     content=types.Content(role="model", parts=list(parts)))

    def user(*parts):
        return Event(author="user",
                     content=types.Content(role="user", parts=list(parts)))

    events = [
        user(types.Part(text="hi")),
        model(types.Part(text="Ok.")),                       # used to hug its text
        model(types.Part(text="A longer reply that runs on for a while, so under "
                              "the old rules it ended at a different x than the "
                              "short one above — the ragged edge being measured.")),
        model(types.Part(function_call=types.FunctionCall(
            id="c1", name="run_bash", args={"command": "ls"}))),
        user(types.Part(function_response=types.FunctionResponse(
            id="c1", name="run_bash",
            response={"exit_code": 0, "stdout": "a\nb\n"}))),
        model(types.Part(function_call=types.FunctionCall(
            id="c2", name="write_file", args={"path": "x.py", "content": "print(1)"}))),
        user(types.Part(function_response=types.FunctionResponse(
            id="c2", name="write_file", response={"status": "ok"}))),
    ]

    async def go():
        svc = FileSessionService(data_dir)
        sess = await svc.create_session(app_name="adk_cc", user_id=project_id,
                                        session_id=session_id)
        for ev in events:
            await svc.append_event(session=sess, event=ev)

    asyncio.run(go())


def main() -> int:
    dist = os.path.join(REPO, "web", "dist-desktop")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print("SKIP: desktop UI not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed."); return 0

    data = tempfile.mkdtemp(prefix="align-")
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
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        sid = "align1"
        # Seed through the SERVICE, not the create-session body: passing events
        # in that body left the thread empty (they were ignored), and a test
        # that measures an empty thread reports alignment it never checked.
        _seed(os.path.join(data), pid, sid)

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.locator(".adk-project-row").first.click(timeout=15000)
            page.wait_for_timeout(1500)
            rows = page.locator(".adk-session-title")
            if rows.count():
                rows.first.click(timeout=6000)
            page.wait_for_timeout(2000)

            # Right edge of every direct child of each left-aligned row.
            edges = page.evaluate("""() => {
              const out = [];
              for (const row of document.querySelectorAll('div.flex.justify-start')) {
                for (const child of row.children) {
                  const r = child.getBoundingClientRect();
                  if (r.width < 40 || r.height < 8) continue;   // icons, spacers
                  out.push({right: Math.round(r.right), width: Math.round(r.width),
                            text: (child.innerText || '').slice(0, 28).replace(/\\n/g, ' ')});
                }
              }
              return out;
            }""")
            print(f"    measured {len(edges)} left-aligned rows")
            for e in edges:
                print(f"      right={e['right']:5d} w={e['width']:5d}  {e['text']!r}")
            check("there are rows to compare", len(edges) >= 3,
                  f"only {len(edges)} — the thread did not render")
            if len(edges) >= 3:
                rights = [e["right"] for e in edges]
                spread = max(rights) - min(rights)
                check("every row ends at the same x", spread <= 2,
                      f"right edges span {spread}px: {sorted(set(rights))}")
            page.screenshot(path=os.path.join(data, "thread.png"), full_page=True)
            with open(os.path.join(data, "edges.json"), "w") as fh:
                json.dump(edges, fh, indent=2)
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
