"""E2E: ACTUAL rendered sizes of the HTML artifact preview.

Distinct from e2e_interactive_html_preview.py (which proves the sandbox
SECURITY property). This one proves the LAYOUT the user asked for, by
mounting the REAL ArtifactChip/HtmlArtifactPreview in the REAL app and
measuring real pixels — not grepping class names out of the bundle (which
can't catch Tailwind failing to emit a rule, or the preview not actually
equalling the chat bubble).

What it asserts, from getBoundingClientRect in a real browser:
  1. WIDTH: the preview iframe width == the agent chat-bubble width
     (both capped at max-w-[80%]) — "match the chat bubble", flush column.
     Also == 80% of the message-row width, and clearly NOT ~90%
     (regression guard against the earlier widen).
  2. HEIGHT: the preview iframe height == clamp(70vh, 320, 640) — i.e. the
     h-[70vh] max-h-[640px] min-h-[320px] from PR #55 actually applies, and
     is clearly TALLER than the old h-96 (384px).

To mount the real component deterministically WITHOUT a slow/nondeterministic
live model turn, we seed a session whose event log already contains an
artifactDelta (ADK persists seeded events — verified) plus a long agent text
message (so the bubble hits its 80% max), then click that session and
measure. The artifact blob is uploaded via the same route the frontend uses.

Run directly:

    .venv/bin/python tests/e2e_html_preview_sizes.py

Requires: npm, the venv (playwright + requests), web/ deps installed.
Builds web/dist once and restores it.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import time

import requests
from playwright.sync_api import sync_playwright

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(REPO, "web")
PORT = 8772
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "tok"  # maps to alice:local
USER = "alice"
APP = "adk_cc"

VIEWPORT = {"width": 1280, "height": 900}  # 0.7*900 = 630 → inside [320,640]

# A trivial HTML artifact — content is irrelevant to layout; we only measure
# the frame box. Kept tiny so the fetch is instant.
HTML = "<!doctype html><html><body><h1>size probe</h1></body></html>"

# Long agent message → forces the MessageBubble inner div to its max-w-[80%].
MARKER = "BUBBLEPROBE"
LONG_TEXT = (MARKER + " " + ("lorem ipsum dolor sit amet " * 60)).strip()


def _build() -> None:
    env = dict(os.environ)
    # allow-scripts is layout-irrelevant; build with it ON to match prod.
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


def _hdr():
    return {"Authorization": f"Bearer {TOKEN}"}


def _seed_session() -> str:
    """Create a session pre-seeded with (a) a long agent text message and
    (b) an artifactDelta for chart.html v0. Returns the session id."""
    events = [
        {
            "id": "evt-text-1", "author": "agent", "invocationId": "inv1",
            "content": {"role": "model", "parts": [{"text": LONG_TEXT}]},
        },
        {
            "id": "evt-artifact-1", "author": "agent", "invocationId": "inv1",
            "actions": {"artifactDelta": {"chart.html": 0}},
            "content": {"role": "model", "parts": [{"text": "saved chart.html"}]},
        },
    ]
    r = requests.post(
        f"{BASE}/apps/{APP}/users/{USER}/sessions",
        headers=_hdr(), json={"events": events}, timeout=10,
    )
    r.raise_for_status()
    return r.json()["id"]


def _save_artifact(session_id: str) -> None:
    """Upload chart.html (v0) via the same route the frontend uses."""
    b64 = base64.b64encode(HTML.encode()).decode()
    body = {
        "filename": "chart.html",
        "artifact": {"inlineData": {"mimeType": "text/html", "data": b64}},
    }
    r = requests.post(
        f"{BASE}/apps/{APP}/users/{USER}/sessions/{session_id}/artifacts",
        headers=_hdr(), json=body, timeout=10,
    )
    if r.status_code // 100 != 2:
        raise RuntimeError(f"artifact save failed {r.status_code}: {r.text[:200]}")


# Measured in-page: iframe box, bubble box, row width, viewport height.
_MEASURE = """
() => {
  const iframe = document.querySelector('iframe');
  if (!iframe) return { error: 'no iframe' };
  const ir = iframe.getBoundingClientRect();
  // The agent bubble: a div whose class carries max-w-[80%] AND whose text
  // contains the marker (disambiguates it from the chip's own max-w-[80%]).
  const bubble = [...document.querySelectorAll('div')].find(
    d => d.className &&
         String(d.className).includes('max-w-[80%]') &&
         d.textContent.includes('BUBBLEPROBE'));
  if (!bubble) return { error: 'no bubble' };
  const br = bubble.getBoundingClientRect();
  // The message row = the bubble's flex parent → full Thread content width.
  const row = bubble.parentElement.getBoundingClientRect();
  return {
    iframeW: ir.width, iframeH: ir.height,
    bubbleW: br.width, rowW: row.width,
    vh: window.innerHeight,
  };
}
"""


def main() -> int:
    backup = os.path.join(WEB, "dist.sizes-backup")
    dist = os.path.join(WEB, "dist")
    if os.path.isdir(dist):
        if os.path.isdir(backup):
            shutil.rmtree(backup)
        shutil.move(dist, backup)
    data_dir = os.path.join(REPO, ".workspace", "html-sizes-artifacts")
    if os.path.isdir(data_dir):
        shutil.rmtree(data_dir)
    os.makedirs(data_dir, exist_ok=True)

    proc = None
    try:
        print("building bundle…")
        _build()
        proc = _start_server(data_dir)

        sid = _seed_session()
        _save_artifact(sid)
        print(f"seeded session {sid[:18]} with artifact + long bubble")

        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page(viewport=VIEWPORT)
            pg.goto(BASE + "/")
            pg.evaluate(
                "(t)=>{localStorage.setItem('adk_cc.token',t);"
                "localStorage.setItem('adk_cc.user','alice')}",
                TOKEN,
            )
            pg.reload()
            pg.wait_for_load_state("networkidle")
            # Click the seeded session row (rail shows id.slice(0,18)).
            pg.get_by_text(sid[:18]).first.click()
            pg.wait_for_selector("iframe", timeout=10000)
            pg.wait_for_timeout(500)  # let layout settle
            m = pg.evaluate(_MEASURE)
            b.close()

        if "error" in m:
            print(f"FAIL: measurement error: {m['error']}")
            return 1

        iframeW, iframeH = m["iframeW"], m["iframeH"]
        bubbleW, rowW, vh = m["bubbleW"], m["rowW"], m["vh"]
        expectedW = 0.80 * rowW
        expectedH = min(0.70 * vh, 640)
        expectedH = max(expectedH, 320)
        print(
            f"\nmeasured: iframe={iframeW:.1f}x{iframeH:.1f}  "
            f"bubble.w={bubbleW:.1f}  row.w={rowW:.1f}  vh={vh}\n"
            f"expected: width≈{expectedW:.1f} (80% of row), "
            f"height≈{expectedH:.1f} (clamp 70vh→[320,640])"
        )

        ok = True

        def check(name, cond, detail):
            nonlocal ok
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}: {detail}")
            ok = ok and cond

        # 1. WIDTH — preview matches the chat bubble (the user's ask).
        check("preview width == bubble width",
              abs(iframeW - bubbleW) <= 2.0,
              f"|{iframeW:.1f} - {bubbleW:.1f}| = {abs(iframeW-bubbleW):.1f}px (≤2)")
        # 2. WIDTH — both are 80% of the message row.
        check("preview width == 80% of row",
              abs(iframeW - expectedW) <= 4.0,
              f"|{iframeW:.1f} - {expectedW:.1f}| = {abs(iframeW-expectedW):.1f}px (≤4)")
        # 3. WIDTH — regression guard: NOT the 90% widen.
        check("preview width is NOT ~90%",
              iframeW < 0.85 * rowW,
              f"{iframeW:.1f} < {0.85*rowW:.1f} (85% of row)")
        # 4. HEIGHT — h-[70vh] clamp applied.
        check("preview height == clamp(70vh,320,640)",
              abs(iframeH - expectedH) <= 3.0,
              f"|{iframeH:.1f} - {expectedH:.1f}| = {abs(iframeH-expectedH):.1f}px (≤3)")
        # 5. HEIGHT — clearly taller than the old h-96 (384px).
        check("preview height > old h-96 (384px)",
              iframeH > 384 + 20,
              f"{iframeH:.1f} > 404")

        print("\n" + ("html-preview-sizes e2e PASSED" if ok
                       else "html-preview-sizes e2e FAILED"))
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
