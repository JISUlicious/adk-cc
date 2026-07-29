"""Browser check: the plan approval card renders Approve / Revise / Deny."""
import json, os, subprocess, tempfile, time, requests

REPO = "/Users/jisu/data/workspace/ref/claude-code-leak/adk-cc"
PORT = 8951; BASE = f"http://127.0.0.1:{PORT}"
data = tempfile.mkdtemp(prefix="plan-ui-")
proj = os.path.join(data, "project"); os.makedirs(proj, exist_ok=True)
subprocess.run(["git", "init", "-q", proj], capture_output=True)
env = dict(os.environ); env.update({
    "ADK_CC_AGENTS_DIR": f"{REPO}/agents", "ADK_CC_ALLOW_NO_AUTH": "1",
    "ADK_CC_DESKTOP": "1", "ADK_CC_DESKTOP_DATA": data,
    "ADK_CC_TENANCY_MODE": "single", "ADK_CC_GLOBAL_TENANT_ID": "local",
    "ADK_CC_SERVE_UI": "1", "ADK_CC_UI_DIST": f"{REPO}/web/dist-desktop",
    "ADK_CC_SANDBOX_BACKEND": "noop", "ADK_CC_SESSION_DSN": "sqlite:///" + data + "/s.db",
    "ADK_CC_PERMISSION_MODE": "plan"})
for _k in ("ADK_CC_API_KEY", "ADK_CC_SKIP_DOTENV", "ADK_CC_SKIP_CONFIG_CHECK"):
    env.pop(_k, None)          # a live turn needs the REAL endpoint config
p = subprocess.Popen([f"{REPO}/.venv/bin/uvicorn", "adk_cc.service.server:make_app", "--factory",
                      "--host", "127.0.0.1", "--port", str(PORT)],
                     cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ok = fails = 0
def check(n, c, d=""):
    global ok, fails
    print(f"  [{'PASS' if c else 'FAIL'}] {n}" + (f" — {d}" if d and not c else ""))
    ok, fails = (ok+1, fails) if c else (ok, fails+1)
try:
    for _ in range(80):
        try:
            if requests.get(BASE+"/list-apps", timeout=2).ok: break
        except Exception: time.sleep(0.25)
    pid = requests.post(BASE+"/desktop/projects", json={"path": proj}, timeout=10).json()["project"]["id"]
    sid = "planui"
    base = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
    # A REAL plan-mode turn: the model plans, then asks for approval. The card
    # under test is whatever the tool actually produced, not a fixture.
    requests.post(base, json={}, timeout=30)
    requests.patch(base, json={"state_delta": {
        "model_endpoint": "chatgpt-codex", "model_id": "chatgpt-codex/gpt-5.4-mini",
        "permission_mode": "plan"}}, timeout=30)
    t = requests.post(f"{BASE}/api/turns", timeout=60, json={
        "appName": "adk_cc", "userId": pid, "sessionId": sid,
        "newMessage": {"role": "user", "parts": [{"text":
            "Plan (do not implement) adding a --verbose flag to a CLI in this "
            "repo. Write the plan and ask me to approve it."}]}}).json()
    for _ in range(90):
        time.sleep(4)
        st = requests.get(f"{BASE}/api/turns/{t['turn_id']}", timeout=30).json()
        if st["status"] != "running": break
    sess = requests.get(base, timeout=30).json()
    tools = [pt["functionCall"]["name"] for e in sess["events"]
             for pt in ((e.get("content") or {}).get("parts") or []) if pt.get("functionCall")]
    print("    turn:", st["status"], tools)
    check("the turn asked for approval", any("confirmation" in n or "plan" in n for n in tools), tools)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page(viewport={"width": 1280, "height": 900})
        page.goto(BASE+"/", wait_until="networkidle"); page.wait_for_timeout(1200)
        page.locator(".adk-project-row").first.click(timeout=8000); page.wait_for_timeout(2500)
        rows = page.locator(".adk-session-title")
        for i in range(rows.count()):
            rows.nth(i).click(timeout=4000); page.wait_for_timeout(2500)
            if "Approve" in page.inner_text("body"): break
        text = page.inner_text("body")
        for label in ("Approve", "Revise", "Deny"):
            check(f"the {label} button is rendered", label in text, text[-300:])
        page.screenshot(path=os.path.join(data, "plan-buttons.png"), full_page=True)
        print("    screenshot:", os.path.join(data, "plan-buttons.png"))
        b.close()
finally:
    p.terminate()
print(f"\n{ok} passed, {fails} failed")
