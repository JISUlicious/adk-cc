"""E2E (Task 1): the knowledge-graph page loads, both tabs render graph data
from the backend, in a real browser. Model-free (seeds stores directly; no agent
turns). Skips if web/dist or playwright unavailable.

Run: .venv/bin/python tests/e2e_knowledge_ui.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

os.environ["ADK_CC_SKIP_DOTENV"] = "1"
os.environ["ADK_CC_API_KEY"] = "stub"
for _k in ("ADK_CC_WIKI_STORE_URI", "ADK_CC_MEMORY_STORE_URI"):
    os.environ.pop(_k, None)

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8790
BASE = f"http://127.0.0.1:{PORT}"
SHOT = "/tmp/knowledge_ui.png"


def main() -> int:
    if not os.path.isfile(os.path.join(REPO, "web", "dist", "index.html")):
        print("SKIP: web/dist not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    root = tempfile.mkdtemp(prefix="kui-")
    # seed wiki (2 linked pages) + memory (consolidated) for 'local'
    from adk_cc.wiki import WikiStore
    from adk_cc.wiki.page import Page
    from adk_cc.memory import MemoryStore, consolidate_user
    w = WikiStore.for_tenant("local", root=root).ensure()
    w.write_domain_page(Page("gpu", {"title": "GPU", "sources": ["s1"]},
                             "GPUs use SIMT. See [[cpu]].\n"))
    w.write_domain_page(Page("cpu", {"title": "CPU", "sources": ["s2"]},
                             "CPUs use deep pipelines.\n"))
    m = MemoryStore.for_tenant("local", root=root)
    for _ in range(2):
        m.add_episodic("local", "User deploys to Fly.io.", topic="deploy-target")
    consolidate_user(m, "local")

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_SERVE_UI": "1",
        "ADK_CC_KNOWLEDGE_UI": "1", "ADK_CC_WIKI": "1", "ADK_CC_MEMORY": "1",
        "ADK_CC_WIKI_ROOT": root, "ADK_CC_MEMORY_ROOT": root,
        "ADK_CC_WORKSPACE_ROOT": root,
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    try:
        for _ in range(60):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.5)
        # backend sanity
        wg = requests.get(BASE + "/api/knowledge/wiki/graph", timeout=5).json()
        mg = requests.get(BASE + "/api/knowledge/memory/graph", timeout=5).json()
        print(f"  wiki graph: {len(wg['nodes'])} nodes / {len(wg['links'])} links")
        print(f"  memory graph: {len(mg['nodes'])} nodes")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            page.goto(BASE + "/knowledge", wait_until="networkidle")
            page.wait_for_selector("text=Knowledge graph", timeout=15000)
            time.sleep(1)
            header = page.content()
            wiki_nodes = int((re.search(r"(\d+) nodes", header) or [0, "0"])[1])
            print(f"  [{'PASS' if wiki_nodes > 0 else 'FAIL'}] wiki tab shows {wiki_nodes} nodes")
            # re-center button present + clickable
            recenter = page.locator("button:has-text('Re-center')")
            recenter_ok = recenter.count() > 0
            if recenter_ok:
                recenter.first.click()
                time.sleep(0.5)
            print(f"  [{'PASS' if recenter_ok else 'FAIL'}] re-center button present")
            # switch to Memory tab
            page.click("text=memory")
            time.sleep(1.5)
            mem_nodes = int((re.search(r"(\d+) nodes", page.content()) or [0, "0"])[1])
            print(f"  [{'PASS' if mem_nodes > 0 else 'FAIL'}] memory tab shows {mem_nodes} nodes")

            # click a MEMORY node (grid scan) → detail pane shows the item
            canvas = page.locator("canvas").first
            b = canvas.bounding_box()
            mx, my = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
            mem_detail = False
            for r in range(0, 200, 24):
                if mem_detail:
                    break
                for dx in range(-r, r + 1, 24):
                    for dy in range(-r, r + 1, 24):
                        if r != 0 and max(abs(dx), abs(dy)) != r:
                            continue
                        page.mouse.click(mx + dx, my + dy)
                        time.sleep(0.15)
                        if page.locator("text=Click a node to view").count() == 0:
                            mem_detail = True
                            break
                    if mem_detail:
                        break
            time.sleep(0.3)
            # the memory detail pane shows the topic ("deploy-target") + text
            shows_item = mem_detail and page.locator("aside:has-text('deploy')").count() > 0
            print(f"  [{'PASS' if shows_item else 'FAIL'}] memory node click → detail pane shows the item")
            page.screenshot(path=SHOT, full_page=False)
            print(f"  screenshot: {SHOT}")
            ok = wiki_nodes > 0 and mem_nodes > 0 and recenter_ok and shows_item
            browser.close()
        print("\nknowledge UI e2e " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
