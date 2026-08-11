"""UI e2e (#129-2): wiki curation from the knowledge page (Playwright).

Full loop against a real server (dev-token auth, admin panel + wiki on):
open /knowledge as an admin, click the (only) page node on the canvas,
Edit → Save (stamps human_edited server-side), then a conflicting inbox
note + librarian run held for review, adjudicated ACCEPT from the Review
pane, and a final librarian run applies it. Delete flow last.

  .venv/bin/python tests/e2e_wiki_admin_ui.py

No model calls — the librarian runs with an injected classifier.
Skips cleanly without web/dist or playwright.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")

import requests

PORT = 8953
BASE = f"http://127.0.0.1:{PORT}"
OLD = "All deploys go through the staging cluster first."
EDIT = "CORRECTED BY OWNER: deploys require BOTH staging and a canary."
NEW = "Deploys now go directly to prod; staging was retired."

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + str(detail)) if detail and not ok else ''}")
    if ok:
        _passed += 1
    else:
        _failed += 1


_LIB_CODE = """
import asyncio, json, os, sys
sys.path.insert(0, os.environ["ADK_CC_AGENTS"])
from adk_cc.wiki import WikiStore, conflict
from adk_cc.wiki.conflict import Verdict
from adk_cc.wiki.librarian import Librarian
st = WikiStore.for_tenant("local").ensure()
def classify(claim, page):
    return Verdict(conflict.SUPERSESSION, reason="newer info")
r = asyncio.run(Librarian(st, classifier=classify).run())
print("ACTIONS:" + json.dumps(r.actions))
"""


def _librarian_run(wiki_root):
    """Run the librarian in a SUBPROCESS (sync-Playwright owns this
    process's event loop, so asyncio.run would explode here)."""
    import json

    env = dict(os.environ)
    env.update({"ADK_CC_WIKI_ROOT": wiki_root,
                "ADK_CC_AGENTS": str(REPO / "agents"),
                "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
                "ADK_CC_API_KEY": "stub"})
    out = subprocess.run([str(REPO / ".venv/bin/python"), "-c", _LIB_CODE],
                         env=env, capture_output=True, text=True, timeout=120)
    for line in out.stdout.splitlines():
        if line.startswith("ACTIONS:"):
            return None, type("R", (), {"actions": json.loads(line[8:])})()
    raise RuntimeError(f"librarian subprocess failed: {out.stdout[-400:]} "
                       f"{out.stderr[-400:]}")


def main() -> int:  # noqa: PLR0915
    dist = os.path.join(REPO, "web", "dist")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print("SKIP: web UI not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    data = tempfile.mkdtemp(prefix="wikiadminui-")
    wiki_root = os.path.join(data, "wiki")

    # Seed ONE domain page (so the canvas has a single, centered node).
    os.environ["ADK_CC_WIKI_ROOT"] = wiki_root
    from adk_cc.wiki import WikiStore
    from adk_cc.wiki.page import Page

    st = WikiStore.for_tenant("local").ensure()
    st.write_domain_page(Page(slug="deploy-process",
                              frontmatter={"title": "Deploy process"},
                              body=OLD + "\n"))

    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_AGENTS_DIR": str(REPO / "agents"),
        "ADK_CC_DATA_DIR": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local",
        "ADK_CC_SERVE_UI": "1", "ADK_CC_UI_DIST": dist,
        "ADK_CC_WIKI": "1", "ADK_CC_WIKI_ROOT": wiki_root,
        "ADK_CC_KNOWLEDGE_UI": "1", "ADK_CC_ADMIN_PANEL": "1",
        "ADK_CC_AUTH_TOKENS": "admintok=alice:local:admin,usertok=bob:local",
        "ADK_CC_API_KEY": "stub",
    })
    proc = subprocess.Popen(
        [str(REPO / ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(REPO), env=env,
        stdout=open(os.path.join(data, "server.log"), "w"),
        stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            page.goto(BASE + "/", wait_until="domcontentloaded")
            page.evaluate(
                "(t) => { localStorage.setItem('adk_cc.token', t);"
                " localStorage.setItem('adk_cc.user', 'alice'); }",
                "admintok")

            def open_node():
                """Click the single node: zoomToFit centers it; probe a
                small grid around the canvas center until the pane loads."""
                page.goto(BASE + "/knowledge", wait_until="networkidle")
                page.wait_for_timeout(2500)  # engine settle + recenter
                canvas = page.locator("canvas").first
                box = canvas.bounding_box()
                cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                for dx in (0, -8, 8, -16, 16):
                    for dy in (0, -8, 8):
                        page.mouse.click(cx + dx, cy + dy)
                        page.wait_for_timeout(400)
                        if page.get_by_text("Deploy process",
                                            exact=False).count() > 0:
                            return True
                return False

            check("node click opens the page detail", open_node())

            # Edit → Save → curated chip + persisted body.
            page.get_by_role("button", name="Edit").click()
            edit = page.locator("[data-wiki-page-edit]")
            check("edit textarea appears", edit.count() > 0)
            edit.fill(EDIT)
            page.get_by_role("button", name="Save", exact=True).click()
            page.wait_for_timeout(1500)
            check("saved body renders", page.get_by_text(
                "CORRECTED BY OWNER", exact=False).count() > 0)
            check("curated chip shown",
                  page.get_by_text("curated", exact=False).count() > 0)
            p = st.read_domain_page("deploy-process")
            check("backend: human_edited stamped by the UI save",
                  bool(p.frontmatter.get("human_edited"))
                  and EDIT in p.body)

            # Conflicting note + librarian → held; Review pane accepts it.
            st.add_inbox("u1", NEW, topic="deploy-process")
            _, report = _librarian_run(wiki_root)
            check("librarian holds the conflicting note",
                  report.actions.get("human_edit_protected", 0) == 1
                  and EDIT in st.read_domain_page("deploy-process").body,
                  report.actions)

            page.reload(wait_until="networkidle")
            page.wait_for_timeout(2000)
            rb = page.get_by_role("button", name="Review (1)")
            check("review button shows the pending count", rb.count() > 0)
            rb.click()
            page.wait_for_timeout(500)
            item = page.locator("[data-review-item]")
            check("review pane lists the held claim", item.count() == 1)
            page.screenshot(path=os.path.join(data, "review.png"))
            item.get_by_role("button", name="Accept").click()
            page.wait_for_timeout(1500)
            check("queue empties after accept",
                  page.locator("[data-review-item]").count() == 0)
            _, _ = _librarian_run(wiki_root)
            check("accepted claim lands on the page after the next run",
                  NEW in st.read_domain_page("deploy-process").body)

            # Delete flow: two-click confirm, node gone from the graph.
            check("reopen node after accept", open_node())
            page.get_by_role("button", name="Delete", exact=True).click()
            page.get_by_role("button", name="Really delete").click()
            page.wait_for_timeout(1500)
            check("backend: page deleted",
                  st.read_domain_page("deploy-process") is None)
            page.screenshot(path=os.path.join(data, "after-delete.png"))
            browser.close()
        print(f"    screenshots: {data}/review.png, {data}/after-delete.png")
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
