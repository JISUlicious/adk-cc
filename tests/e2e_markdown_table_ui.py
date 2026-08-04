"""W6.4: a markdown table in an answer behaves like a table.

Checked in a browser, and geometrically where geometry is the point — the
dataset-panel work in this session had text assertions pass while the table was
visibly bleeding out of its column, so "the text is present" is not evidence
that a table is usable.

Uses a REAL turn: the model is asked for a markdown table, and whatever it
produces is what gets sorted. A fixture would test my own string, not the
renderer's path from model output to DOM.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_markdown_table_ui.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8955
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
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    data = tempfile.mkdtemp(prefix="mdtable-")
    proj = os.path.join(data, "project")
    os.makedirs(proj, exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)

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
        sid = f"tbl{int(time.time())}"
        base = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
        requests.post(base, json={}, timeout=30)
        requests.patch(base, json={"state_delta": {
            "model_endpoint": "chatgpt-codex",
            "model_id": "chatgpt-codex/gpt-5.4-mini",
            "permission_mode": "bypassPermissions"}}, timeout=30)
        t = requests.post(f"{BASE}/api/turns", timeout=60, json={
            "appName": "adk_cc", "userId": pid, "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text":
                "Reply with ONLY a markdown table, no prose: 18 rows, columns "
                "region | product | revenue | units. Use varied numbers and "
                "long product names. Do not sort it."}]}}).json()
        for _ in range(90):
            time.sleep(4)
            st = requests.get(f"{BASE}/api/turns/{t['turn_id']}", timeout=30).json()
            if st["status"] != "running":
                break
        check("the turn produced an answer", st["status"] == "done", st.get("error"))

        # Second turn, same session: a table drawn with FULLWIDTH bars.
        # Reported live — the model sometimes emits U+FF5C instead of `|` and
        # the table collapses into a paragraph. Asked for explicitly, because
        # that is the only reliable way to get the character; if the model
        # declines, the check is skipped rather than quietly passed.
        FW = "\uFF5C"
        t2 = requests.post(f"{BASE}/api/turns", timeout=60, json={
            "appName": "adk_cc", "userId": pid, "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text":
                "Output ONLY a markdown table with 3 rows and columns city "
                f"and country. Use the fullwidth vertical bar '{FW}' (U+FF5C) "
                "as the column separator on EVERY line, including the header "
                "separator row. No prose, no code fence."}]}}).json()
        for _ in range(90):
            time.sleep(4)
            st2 = requests.get(f"{BASE}/api/turns/{t2['turn_id']}",
                               timeout=30).json()
            if st2["status"] != "running":
                break
        emitted_fw = FW in json.dumps(
            requests.get(base, timeout=30).json().get("events") or [])

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.locator(".adk-project-row").first.click(timeout=8000)
            page.wait_for_timeout(2500)
            rows = page.locator(".adk-session-title")
            for i in range(rows.count()):
                rows.nth(i).click(timeout=4000)
                page.wait_for_timeout(2500)
                if page.locator("table").count():
                    break

            tbl = page.locator("table").first
            check("the answer rendered as a real <table>", tbl.count() > 0)
            if not tbl.count():
                b.close(); return 1

            # The fullwidth table must ALSO have become a real table. Counted
            # rather than re-navigated: both answers live in this session, so
            # the repair working means two tables on the page.
            if not emitted_fw:
                print("  [skip] the model did not emit a fullwidth bar")
            else:
                n_tables = page.locator("table").count()
                check("the fullwidth-bar table also rendered as a <table>",
                      n_tables >= 2, f"{n_tables} table(s) in the thread")
                check("no raw fullwidth bar survives in the rendered text",
                      FW not in page.inner_text("body"))

            body_rows = page.locator("table tbody tr")
            n = body_rows.count()
            check("all rows rendered", n >= 10, f"{n} rows")

            # sorting: click a numeric header and the order must change
            heads = page.locator("table thead th button")
            check("headers are clickable sort controls", heads.count() >= 3, heads.count())
            before = [body_rows.nth(i).inner_text() for i in range(min(n, 6))]
            idx = None
            for i in range(heads.count()):
                if "revenue" in heads.nth(i).inner_text().lower():
                    idx = i
                    break
            check("a revenue column exists to sort by", idx is not None)
            if idx is not None:
                heads.nth(idx).click()
                page.wait_for_timeout(400)
                after = [page.locator("table tbody tr").nth(i).inner_text()
                         for i in range(min(n, 6))]
                check("clicking a header reorders the rows", before != after,
                      "order unchanged after sort")
                vals = []
                for i in range(page.locator("table tbody tr").count()):
                    cell = page.locator("table tbody tr").nth(i).locator("td").nth(idx)
                    txt = cell.inner_text().replace(",", "").replace("$", "").strip()
                    try:
                        vals.append(float(txt))
                    except ValueError:
                        pass
                check("ascending sort is actually ascending",
                      vals == sorted(vals), vals[:8])
                # third click restores the agent's order
                heads.nth(idx).click(); page.wait_for_timeout(200)
                heads.nth(idx).click(); page.wait_for_timeout(400)
                restored = [page.locator("table tbody tr").nth(i).inner_text()
                            for i in range(min(n, 6))]
                check("a third click restores the original order",
                      restored == before, "original order not restored")

            # numeric alignment + containment
            align = page.locator("table tbody tr").first.locator("td").nth(idx or 2).evaluate(
                "el => getComputedStyle(el).textAlign")
            check("numeric column is right-aligned", align == "right", align)

            wrap = tbl.locator("xpath=..").bounding_box() or {}
            bubble = page.locator("table").first.evaluate(
                "el => { const b = el.closest('[class*=rounded]')?.parentElement;"
                " return b ? b.getBoundingClientRect().width : 0 }")
            doc_w = page.evaluate("document.documentElement.scrollWidth")
            check("the page never scrolls horizontally", doc_w <= 1282, f"scrollWidth={doc_w}")
            check("the table stays inside the message column",
                  (wrap.get("width") or 0) <= (bubble or 1e9) + 2,
                  f"table {wrap.get('width')} vs column {bubble}")

            page.screenshot(path=os.path.join(data, "table.png"), full_page=True)
            print(f"    screenshot: {os.path.join(data, 'table.png')}")
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
