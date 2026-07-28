"""UI check: the Datasets strip is visible and reports what is in data/."""
import os, subprocess, tempfile, time, requests
REPO = "/Users/jisu/data/workspace/ref/claude-code-leak/adk-cc"
PORT = 8941; BASE = f"http://127.0.0.1:{PORT}"
data = tempfile.mkdtemp(prefix="ds-ui-")
proj = os.path.join(data, "project"); os.makedirs(os.path.join(proj, "data"), exist_ok=True)
subprocess.run(["git", "init", "-q", proj], capture_output=True)
open(os.path.join(proj, "data", "sales.csv"), "w").write("month,revenue\n2026-01,1200\n")
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
        check("it reports the dataset already in data/", "1 in data/" in text,
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
