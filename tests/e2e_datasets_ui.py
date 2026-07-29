"""UI check: the Datasets strip is visible and reports what is in data/."""
import os, subprocess, tempfile, time, requests
REPO = "/Users/jisu/data/workspace/ref/claude-code-leak/adk-cc"
PORT = 8941; BASE = f"http://127.0.0.1:{PORT}"
data = tempfile.mkdtemp(prefix="ds-ui-")
proj = os.path.join(data, "project"); os.makedirs(os.path.join(proj, "data"), exist_ok=True)
subprocess.run(["git", "init", "-q", proj], capture_output=True)
open(os.path.join(proj, "data", "sales.csv"), "w").write("month,revenue\n2026-01,1200\n")
# A WIDE dataset with long values: the layout case most likely to blow the
# panel out horizontally. A 2-column fixture proves nothing about that.
_wide_cols = [f"column_with_a_long_name_{i}" for i in range(12)]
with open(os.path.join(proj, "data", "wide.csv"), "w") as _f:
    _f.write(",".join(_wide_cols) + "\n")
    for _r in range(200):
        _f.write(",".join(
            [f"value-{_r}-{_c}-and-then-some-more-text" for _c in range(11)] + [""]) + "\n")
env = dict(os.environ); env.update({
    "ADK_CC_AGENTS_DIR": f"{REPO}/agents", "ADK_CC_ALLOW_NO_AUTH": "1",
    "ADK_CC_DESKTOP": "1", "ADK_CC_DESKTOP_DATA": data,
    "ADK_CC_TENANCY_MODE": "single", "ADK_CC_GLOBAL_TENANT_ID": "local",
    "ADK_CC_SERVE_UI": "1", "ADK_CC_UI_DIST": f"{REPO}/web/dist-desktop",
    "ADK_CC_SANDBOX_BACKEND": "noop", "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub"})
p = subprocess.Popen([f"{REPO}/.venv/bin/uvicorn", "adk_cc.service.server:make_app", "--factory",
                      "--host", "127.0.0.1", "--port", str(PORT)],
                     cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ok = fails = 0
def check(n, c, d=""):
    global ok, fails
    print(f"  [{'PASS' if c else 'FAIL'}] {n}" + (f" — {d}" if d and not c else ""))
    ok, fails = (ok + 1, fails) if c else (ok, fails + 1)
try:
    for _ in range(80):
        try:
            if requests.get(BASE + "/list-apps", timeout=2).ok: break
        except Exception: time.sleep(0.25)
    pid = requests.post(BASE + "/desktop/projects", json={"path": proj}, timeout=10).json()["project"]["id"]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page(viewport={"width": 1280, "height": 900})
        page.goto(BASE + "/", wait_until="networkidle"); page.wait_for_timeout(1200)
        page.locator(".adk-project-row").first.click(timeout=8000)
        page.wait_for_timeout(3000)
        text = page.inner_text("body")
        check("Datasets strip is shown in the Files panel", "Datasets" in text, text[:200])
        # The panel header names the workspace root once — every path below it
        # is relative to that, and the panel could not answer "which directory?"
        # Long paths are middle-ellipsised for the header, so assert the LEAF is
        # readable and the FULL path is in the tooltip.
        head_line = [l for l in text.split("\n") if "Files" in l][:2]
        check("the workspace leaf is readable beside 'Files'",
              os.path.basename(proj) in text, head_line)
        path_el = page.locator(f"[title='{proj}']").first
        check("the full path is available on hover", path_el.count() > 0, head_line)
        # The header controls must survive a long path — a title that grows
        # until it pushes Undo/History/Refresh off the edge is a regression.
        # Counted geometrically: the Undo tooltip lives on a wrapping <span>,
        # so title-based selectors see only some of the buttons.
        pbox = page.locator("aside, [class*=RightPanel], body").last.bounding_box() or {}
        panel_r = pbox.get("x", 0) + pbox.get("width", 0)
        btns = page.locator("button")
        header_btns, off = 0, 0
        for i in range(btns.count()):
            b = btns.nth(i).bounding_box()
            if not b or b["y"] > 44 or b["x"] < pbox.get("x", 0):
                continue                     # not in this panel's header row
            header_btns += 1
            if b["x"] + b["width"] > panel_r + 1:
                off += 1
        check("header controls stay on screen next to the path",
              header_btns >= 3 and off == 0,
              f"{header_btns} header buttons, {off} pushed off the edge")
        del_icons = page.locator("button[title='Delete']")
        check("the delete control is visible without hovering",
              del_icons.count() > 0 and (del_icons.first.evaluate(
                  "el => parseFloat(getComputedStyle(el).opacity)") or 0) > 0.2,
              "delete icon is hover-only")
        check("it reports the datasets already in data/", "2 in data/" in text,
              [l for l in text.split("\n") if "data/" in l][:3])
        check("the dataset is listed by name", "sales.csv" in text, text[:300])

        # Click it: shape/dtypes/nulls/head must appear WITHOUT asking the agent.
        # The first profile provisions the analysis runtime, so allow for it.
        page.get_by_text("sales.csv", exact=False).first.click(timeout=8000)
        profile_text = ""
        for _ in range(60):
            page.wait_for_timeout(2000)
            profile_text = page.inner_text("body")
            if "rows ×" in profile_text or "cols" in profile_text:
                break
        check("profile shows shape without a turn", "cols" in profile_text,
              profile_text[:300])
        check("profile shows a dtype", "int64" in profile_text or "object" in profile_text
              or "str" in profile_text, profile_text[:300])
        check("profile shows the head row values", "1200" in profile_text,
              profile_text[:400])
        # Text assertions cannot see a panel that renders but overflows or
        # clips. Keep the pixels for review.
        shot = os.path.join(data, "datasets-panel.png")
        page.locator("aside, [class*=RightPanel], body").last.screenshot(path=shot)
        page.screenshot(path=os.path.join(data, "datasets-full.png"), full_page=True)
        print(f"    panel screenshot: {shot}")

        # Wide dataset: the panel must not grow past its column, and the head
        # table must scroll inside it.
        panel_before = page.locator("aside, [class*=RightPanel], body").last.bounding_box()
        page.get_by_text("wide.csv", exact=False).first.click(timeout=8000)
        for _ in range(60):
            page.wait_for_timeout(2000)
            if "12 cols" in page.inner_text("body"):
                break
        wide_text = page.inner_text("body")
        check("wide dataset profiles (12 cols)", "12 cols" in wide_text, wide_text[:200])
        doc_w = page.evaluate("document.documentElement.scrollWidth")
        check("the page does not scroll horizontally", doc_w <= 1280 + 2, f"scrollWidth={doc_w}")
        panel_after = page.locator("aside, [class*=RightPanel], body").last.bounding_box()
        check("the panel keeps its width",
              abs((panel_after or {}).get("width", 0) - (panel_before or {}).get("width", 0)) < 2,
              f"{panel_before} -> {panel_after}")
        # Geometry, not eyeballing: the head table must stay INSIDE the panel.
        panel_box = page.locator("aside, [class*=RightPanel], body").last.bounding_box() or {}
        tbl = page.locator("table").first.bounding_box() or {}
        wrap = page.locator("div.overflow-x-auto").last.bounding_box() or {}
        panel_right = panel_box.get("x", 0) + panel_box.get("width", 0)
        wrap_right = wrap.get("x", 0) + wrap.get("width", 0)
        check("the head table scrolls inside the panel, not past it",
              wrap_right <= panel_right + 1,
              f"wrapper right={wrap_right:.0f} panel right={panel_right:.0f} "
              f"table width={tbl.get('width', 0):.0f}")
        page.screenshot(path=os.path.join(data, "datasets-wide.png"), full_page=True)
        print(f"    wide screenshot: {os.path.join(data, 'datasets-wide.png')}")
        # API round trip through the running server
        q = f"?project_id={pid}&session_id=probe"
        src = os.path.join(data, "extra.parquet"); open(src, "wb").write(b"PAR1")
        r = requests.post(f"{BASE}/desktop/datasets/from-path{q}", json={"path": src}, timeout=15)
        check("ingest via the running server", r.ok and r.json()["dataset"]["format"] == "parquet", r.text[:150])
        check("it landed in the project's data/", os.path.isfile(os.path.join(proj, "data", "extra.parquet")))
        b.close()
finally:
    p.terminate()
print(f"\n{ok} passed, {fails} failed")
raise SystemExit(1 if fails else 0)
