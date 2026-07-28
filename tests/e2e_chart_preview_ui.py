"""E2E (real pixels): does an interactive chart artifact actually RENDER?

W6.1 rests on a claim greps cannot settle — that a JS-drawn chart paints inside
the preview iframe. `SandboxedHtml` runs `sandbox=""` (scripts inert) unless
VITE_ADK_CC_HTML_PREVIEW_ALLOW_SCRIPTS=1 is baked in at build time, and the flag
lives in the REPO-ROOT .env (vite's envDir points one level up) — so grepping
`web/` says "off" while the running app says "on". Only pixels decide.

So: open the file in the desktop UI, screenshot the preview iframe, and measure
how much of it is non-blank. A blank frame and a rendered chart are trivially
distinguishable that way; nothing else in the stack distinguishes them at all.

Run: .venv/bin/python tests/e2e_chart_preview_ui.py
"""

from __future__ import annotations

import os
import statistics
import subprocess
import tempfile
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8938
BASE = f"http://127.0.0.1:{PORT}"

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


# A chart that only exists if JS runs: canvas drawing, no external assets.
# Same mechanism a Plotly bundle needs, in 20 lines instead of 4.8MB.
CHART_HTML = """<!doctype html>
<html><body style="margin:0;background:#fff">
<canvas id="c" width="600" height="360"></canvas>
<script>
const ctx = document.getElementById('c').getContext('2d');
const data = [120, 90, 150, 70, 175, 110, 140, 160];
ctx.fillStyle = '#1f77b4';
data.forEach((v, i) => ctx.fillRect(20 + i * 70, 340 - v, 50, v));
ctx.strokeStyle = '#333'; ctx.beginPath();
ctx.moveTo(10, 345); ctx.lineTo(590, 345); ctx.stroke();
</script>
</body></html>
"""


def _nonblank_fraction(png_bytes: bytes) -> float:
    """Fraction of pixels that differ from the frame's dominant colour."""
    try:
        from PIL import Image
    except Exception:
        return -1.0
    import io

    im = Image.open(io.BytesIO(png_bytes)).convert("L")
    px = list(im.getdata())
    if not px:
        return 0.0
    bg = statistics.mode(px)
    return sum(1 for p in px if abs(p - bg) > 12) / len(px)


def main() -> int:
    dist = os.path.join(REPO, "web", "dist-desktop")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print("SKIP: desktop UI not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0
    try:
        import PIL  # noqa: F401
    except Exception:
        print("SKIP: Pillow unavailable (needed to measure rendered pixels)."); return 0

    data = os.environ.get("ADK_CC_E2E_DATA") or tempfile.mkdtemp(prefix="chart-ui-")
    print(f"  data dir: {data}")
    proj = os.path.join(data, "project")
    reuse = os.path.isdir(os.path.join(proj, ".git"))
    os.makedirs(os.path.join(proj, "analysis"), exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)
    with open(os.path.join(proj, "analysis", "chart.html"), "w") as f:
        f.write(CHART_HTML)

    live = os.environ.get("ADK_CC_LIVE") == "1"
    env = dict(os.environ)
    if live:
        # The model-free path boots with a stub key and SKIP_DOTENV; carrying
        # those into a LIVE run kills every turn ("Connection error" /
        # AuthenticationError) because the real endpoint config never loads.
        for k in ("ADK_CC_API_KEY", "ADK_CC_SKIP_DOTENV", "ADK_CC_SKIP_CONFIG_CHECK"):
            env.pop(k, None)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data,
        "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local",
        "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": dist,
        "ADK_CC_SANDBOX_BACKEND": "noop",
        # persisted so a failing UI assertion can be re-examined without
        # spending another live model turn
        "ADK_CC_SESSION_DSN": "sqlite:///" + os.path.join(data, "sessions.db"),
    })
    if not live:
        env.update({"ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub"})
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

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1200)

            def dump(tag):
                path = os.path.join(data, f"ui-{tag}.png")
                page.screenshot(path=path, full_page=True)
                body = page.inner_text("body")[:600].replace("\n", " | ")
                print(f"    [{tag}] {path}\n        text: {body}")

            # The Files panel only mounts once a project AND a session exist
            # (ChatPage renders RightPanel behind `appName && session`).
            try:
                page.get_by_text(os.path.basename(proj), exact=False).first.click(timeout=5000)
            except Exception:
                dump("no-project")
            page.wait_for_timeout(1500)
            for label in ("New chat", "New session", "New"):
                try:
                    page.get_by_role("button", name=label).first.click(timeout=2000)
                    break
                except Exception:
                    continue
            page.wait_for_timeout(2000)
            dump("after-open")
            try:
                page.get_by_text("analysis", exact=True).first.click(timeout=6000)
                page.wait_for_timeout(500)
                page.get_by_text("chart.html", exact=True).first.click(timeout=6000)
            except Exception as e:  # noqa: BLE001
                dump("no-file-tree")
                check("could reach the file preview in the UI", False,
                      f"{type(e).__name__} — pid={pid}")
                browser.close()
                return 1
            page.wait_for_timeout(1500)

            frames = page.locator("iframe")
            n = frames.count()
            for i in range(n):
                el = frames.nth(i)
                box = el.bounding_box() or {}
                print(f"    iframe[{i}] sandbox={el.get_attribute('sandbox')!r} "
                      f"title={el.get_attribute('title')!r} "
                      f"{int(box.get('width', 0))}x{int(box.get('height', 0))}")
            check("exactly one preview iframe is on the page", n == 1, f"{n} iframes")
            frame_el = frames.first
            frame_el.wait_for(timeout=8000)
            shot = frame_el.screenshot()
            frac = _nonblank_fraction(shot)
            sandbox = frame_el.get_attribute("sandbox")
            print(f"    iframe sandbox={sandbox!r} · non-blank pixels: {frac:.3%}")
            out = os.path.join(data, "preview.png")
            with open(out, "wb") as f:
                f.write(shot)
            print(f"    screenshot: {out}")
            check("the chart actually renders (non-blank pixels)", frac > 0.02,
                  f"{frac:.3%} — a blank frame means scripts are inert "
                  f"(sandbox={sandbox!r})")

            # --- phase 2 (ADK_CC_LIVE=1): the W6.1 TRIGGER, end to end -------
            # A chart the agent writes must appear IN THE CHAT, without the
            # user hunting the file tree. Needs a real model turn, so it is
            # opt-in; the viewport is deliberately below the lg breakpoint so
            # the Files panel is a closed drawer and any iframe on the page
            # can only have come from the conversation.
            if os.environ.get("ADK_CC_LIVE") == "1":
                sid = f"chart-{int(time.time())}"
                base = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
                requests.post(base, json={}, timeout=30)
                requests.patch(base, json={"state_delta": {
                    "model_endpoint": "chatgpt-codex",
                    "model_id": "chatgpt-codex/gpt-5.4-mini",
                    "permission_mode": "bypassPermissions"}}, timeout=30)
                turn = requests.post(f"{BASE}/api/turns", timeout=60, json={
                    "appName": "adk_cc", "userId": pid, "sessionId": sid,
                    "newMessage": {"role": "user", "parts": [{"text":
                        "Write analysis/bars.html — a self-contained page that "
                        "draws a bar chart of [5,9,3,7] on a <canvas> with inline "
                        "JavaScript. No external files."}]}}).json()
                tid = turn["turn_id"]
                for _ in range(90):
                    time.sleep(4)
                    st = requests.get(f"{BASE}/api/turns/{tid}", timeout=30).json()
                    if st["status"] != "running":
                        break
                sess = requests.get(base, timeout=30).json()
                tools = [pt["functionCall"]["name"]
                         for e in sess["events"]
                         for pt in ((e.get("content") or {}).get("parts") or [])
                         if pt.get("functionCall")]
                print(f"    turn {st['status']} · tools: {tools}")
                if st.get("error"):
                    print(f"    turn error: {str(st['error'])[:200]}")
                deltas = [d for e in sess["events"]
                          for d in ((e.get("actions") or {}).get("artifactDelta") or {})]
                check("the written chart was registered as an artifact", bool(deltas), deltas)

                # Navigate with the rail's own selectors (ProjectRail.tsx /
                # SessionList.tsx). Keep the viewport WIDE: below the lg
                # breakpoint the rail goes off-canvas and every click times out
                # "outside of the viewport" — which looks like a missing chip.
                # No file is selected in the Files panel, so any iframe on the
                # page can only come from the conversation.
                page.goto(BASE + "/", wait_until="networkidle")
                page.wait_for_timeout(1500)
                page.locator(".adk-project-row").first.click(timeout=8000)
                page.wait_for_timeout(2500)
                page.locator(".adk-session-title").first.click(timeout=8000)
                page.wait_for_timeout(4000)
                page_text = page.inner_text("body")
                check("the artifact chip appears in the conversation",
                      "bars.html" in page_text, page_text[-200:])
                chat_frames = page.locator("iframe")
                for i in range(chat_frames.count()):
                    el = chat_frames.nth(i)
                    box = el.bounding_box() or {}
                    print(f"    chat iframe[{i}] title={el.get_attribute('title')!r} "
                          f"sandbox={el.get_attribute('sandbox')!r} "
                          f"{int(box.get('width', 0))}x{int(box.get('height', 0))}")
                for marker in ("preview failed", "rendering "):
                    if marker in page_text:
                        idx = page_text.find(marker)
                        print(f"    page says: {page_text[idx:idx+90]!r}")
                dump("chat")
                if chat_frames.count() == 0:
                    dump("no-chip")
                check("the chart is previewed in the CHAT, unprompted",
                      chat_frames.count() > 0, "no iframe in the conversation")
                if chat_frames.count():
                    # The iframe exists before its content paints; an immediate
                    # screenshot caught a blank frame once and looked like a
                    # rendering failure. Poll until pixels appear.
                    shot2, frac2 = b"", 0.0
                    for _ in range(20):
                        shot2 = chat_frames.first.screenshot()
                        frac2 = _nonblank_fraction(shot2)
                        if frac2 > 0.02:
                            break
                        page.wait_for_timeout(500)
                    out2 = os.path.join(data, "chat-preview.png")
                    open(out2, "wb").write(shot2)
                    print(f"    chat preview: {out2} · non-blank {frac2:.3%}")
                    check("the in-chat preview is drawn, not blank", frac2 > 0.02,
                          f"{frac2:.3%}")
            browser.close()
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
