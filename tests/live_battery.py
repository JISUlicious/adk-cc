"""Live dogfooding battery: BOTH shells, real model, extended conversations.

Scenarios (all real turns against the registry model endpoint):
  web:     BUILD (write+run code, 2 turns) · RESEARCH Q&A (5 turns with a
           callback to turn 1) · WIKI round-trip (wiki_add → librarian
           --no-model --personal → search via a turn) · NOTES + /compact
           (record → filler → guided compact → recall) · MEMORY across
           sessions (capture in A, recall in a fresh B)
  desktop: BUILD into the real project dir (in-place workspace) ·
           Q&A continuity (3 turns)

Everything is LEFT BEHIND for inspection: data roots live under
~/adk-cc-battery/{web,desktop}, the two servers KEEP RUNNING after the
battery (web http://127.0.0.1:8961, desktop shell http://127.0.0.1:8963),
and serve-web.sh / serve-desktop.sh are written there to relaunch later.

Run:  ADK_CC_LIVE=1 .venv/bin/python tests/live_battery.py
Skips cleanly without a model endpoint / UI builds / playwright.
"""
from __future__ import annotations

import os
import stat
import subprocess
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = "chatgpt-codex/gpt-5.4-mini"
BATTERY = os.path.expanduser("~/adk-cc-battery")
KEEP_UP = os.environ.get("BATTERY_KEEP_UP", "1") == "1"
_passed = _failed = 0
_results: list[str] = []


def _mkdirs(*paths):
    for p in paths:
        os.makedirs(p, exist_ok=True)
    return paths


def _write_serve_script(name, port, env):
    """A relaunch script so the sessions can be browsed again anytime."""
    lines = ["#!/bin/sh", f"cd {REPO}"]
    for k, v in sorted(env.items()):
        if k.startswith("ADK_CC_"):
            lines.append(f"export {k}='{v}'")
    lines.append(f"exec .venv/bin/uvicorn adk_cc.service.server:make_app "
                 f"--factory --host 127.0.0.1 --port {port}")
    path = os.path.join(BATTERY, name)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def check(shell, name, ok, detail=""):
    global _passed, _failed
    line = f"[{'PASS' if ok else 'FAIL'}] {shell}: {name}" + (
        f" — {str(detail)[:140]}" if detail and not ok else "")
    print("  " + line, flush=True)
    _results.append(line)
    if ok:
        _passed += 1
    else:
        _failed += 1


def boot(shell, port, data, extra_env):
    dist = os.path.join(REPO, "web",
                        "dist" if shell == "web" else "dist-desktop")
    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_MODEL_REGISTRY_FILE": os.path.expanduser(
            "~/.adk-cc-desktop/admin-data/model-endpoints.json"),
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_DATA_DIR": data, "ADK_CC_DESKTOP_DATA": data,
        "ADK_CC_TENANCY_MODE": "single", "ADK_CC_GLOBAL_TENANT_ID": "local",
        "ADK_CC_SANDBOX_BACKEND": "noop", "ADK_CC_NOOP_ACK_HOST_EXEC": "1",
        "ADK_CC_SERVE_UI": "1", "ADK_CC_UI_DIST": dist,
        "ADK_CC_DEFAULT_MODEL": MODEL,
    })
    env.update(extra_env)
    _write_serve_script(f"serve-{shell}.sh", port, env)
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"),
         "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env,
        stdout=open(os.path.join(data, "server.log"), "w"),
        stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if requests.get(base + "/list-apps", timeout=2).ok:
                break
        except Exception:
            time.sleep(0.25)
    return proc, base


def _teardown(proc):
    if KEEP_UP:
        return  # leave the server running for inspection
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def api_turn(base, user, sid, text, expect="", timeout_s=150):
    """A real model turn through the broker API (no UI) — used for the
    extra left-behind sessions. Returns True when the turn finished and the
    session's last model text contains `expect`."""
    try:
        r = requests.post(f"{base}/api/turns", json={
            "appName": "adk_cc", "userId": user, "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text": text}]},
        }, timeout=30)
        if r.status_code != 201:
            return False
        tid = r.json()["turn_id"]
        for _ in range(timeout_s // 2):
            time.sleep(2)
            snap = requests.get(f"{base}/api/turns/{tid}", timeout=10).json()
            if snap.get("status") != "running":
                break
        settle(base, user, sid)
        if not expect:
            return snap.get("status") == "done"
        sess = requests.get(
            f"{base}/apps/adk_cc/users/{user}/sessions/{sid}",
            timeout=15).json()
        texts = []
        for ev in sess.get("events", []):
            for part in (ev.get("content") or {}).get("parts", []) or []:
                if part.get("text"):
                    texts.append(part["text"])
        return any(expect.lower() in t.lower() for t in texts)
    except Exception:
        return False


def make_turn(page, base):
    box = page.locator("textarea.adk-composer-input")

    def turn(text, expect, tries=75):
        """Send a message and wait for the MODEL's reply to contain the
        expected string(s). Matches are counted against a pre-send baseline
        so the user's own bubble can never satisfy the check (the first
        battery run raced itself exactly that way)."""
        exps = list(expect) if isinstance(expect, (list, tuple)) else [expect]
        exps = [e for e in exps if e]
        base_counts = {e: page.get_by_text(e, exact=False).count()
                       for e in exps}
        need = {e: base_counts[e] + (2 if e.lower() in text.lower() else 1)
                for e in exps}
        box.fill(text)
        send = page.get_by_title("Send (Enter)")
        for _ in range(60):  # wait out any still-finishing previous turn
            if send.is_enabled():
                break
            page.wait_for_timeout(1000)
        send.click()
        for _ in range(tries):
            page.wait_for_timeout(2000)
            if not exps:
                if send.is_enabled():
                    return True
                continue
            if all(page.get_by_text(e, exact=False).count() >= need[e]
                   for e in exps):
                return True
        return False

    return turn


def settle(base, user, sid, timeout_s=90):
    """Wait until the server-side turn (post-turn capture etc.) finishes."""
    for _ in range(timeout_s):
        try:
            t = requests.get(
                f"{base}/api/turns/latest?appName=adk_cc&userId={user}"
                f"&sessionId={sid}", timeout=10)
            if not t.ok or t.json().get("status") != "running":
                return
        except Exception:
            return
        time.sleep(1)


def sessions(base, user):
    return requests.get(f"{base}/apps/adk_cc/users/{user}/sessions",
                        timeout=10).json()


def bypass(base, user, sid):
    requests.patch(
        f"{base}/apps/adk_cc/users/{user}/sessions/{sid}",
        json={"state_delta": {"permission_mode": "bypassPermissions"}},
        timeout=15)


def run_web(pw):  # noqa: PLR0915
    data = os.path.join(BATTERY, "web")
    wsroot = os.path.join(BATTERY, "web-workspaces")
    wiki_root = os.path.join(data, "wiki")
    mem_root = os.path.join(data, "memory")
    _mkdirs(data, wsroot)
    proc, base = boot("web", 8961, data, {
        "ADK_CC_WORKSPACE_ROOT": wsroot,
        "ADK_CC_WIKI": "1", "ADK_CC_WIKI_ROOT": wiki_root,
        "ADK_CC_MEMORY": "1", "ADK_CC_MEMORY_ROOT": mem_root,
        "ADK_CC_KNOWLEDGE_UI": "1",
    })
    try:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 950})
        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(1500)

        def new_session():
            page.get_by_role("button", name="New").first.click(timeout=20000)
            page.wait_for_timeout(2000)
            sid = sessions(base, "alice")[0]["id"]
            bypass(base, "alice", sid)
            return sid

        turn = make_turn(page, base)

        # ---- 1. BUILD -------------------------------------------------
        sid = new_session()
        ok = turn("Create a file fib.py in the workspace containing an "
                  "iterative fibonacci function and a __main__ that prints "
                  "fib(20). Run it and tell me the printed number.", "6765",
                  tries=90)
        check("web", "build: writes + runs code, reports fib(20)=6765", ok)
        ok = turn("Now change fib.py to also print the SUM of fib(1)..fib(10) "
                  "on a second line, run it, and give me that sum.", "143",
                  tries=90)
        check("web", "build: follow-up edit + rerun (sum=143)", ok)
        page.screenshot(path=os.path.join(data, "build.png"))

        # ---- 2. RESEARCH / Q&A ---------------------------------------
        new_session()
        qa = [
            ("In one short paragraph: what problem does a CPU branch "
             "predictor solve?", ["branch"]),
            ("And what is a TAGE predictor, briefly?", ["TAGE"]),
            ("How does mispredict penalty relate to pipeline depth? One "
             "paragraph.", ["pipeline"]),
            ("Give me a two-line summary comparing static vs dynamic "
             "prediction.", ["static"]),
            ("Finally, restate the PROBLEM from my FIRST question in this "
             "conversation, in one sentence.", ["branch"]),
        ]
        got = sum(1 for q, exp in qa if turn(q, exp, tries=60))
        check("web", f"research: 5-turn Q&A coherent ({got}/5), incl. "
              "turn-1 callback", got == 5, f"{got}/5")
        page.screenshot(path=os.path.join(data, "qna.png"))

        # ---- 3. WIKI round-trip --------------------------------------
        new_session()
        ok = turn("Use the wiki_add tool to record this domain fact — "
                  "topic 'acme-deploy-pipeline': 'The ACME deploy pipeline "
                  "requires staging sign-off before prod.' Then confirm "
                  "what you stored.", ["acme", "staging"], tries=60)
        check("web", "wiki: agent captures a note via wiki_add", ok)
        lib_env = dict(os.environ,
                       ADK_CC_SKIP_DOTENV="1", ADK_CC_SKIP_CONFIG_CHECK="1",
                       ADK_CC_API_KEY="stub", ADK_CC_WIKI_ROOT=wiki_root,
                       PYTHONPATH=os.path.join(REPO, "agents"))
        r = subprocess.run(
            [os.path.join(REPO, ".venv/bin/python"),
             os.path.join(REPO, "scripts/wiki_librarian.py"),
             "--root", wiki_root, "--no-model", "--personal"],
            env=lib_env, capture_output=True, text=True, timeout=120)
        dom = os.path.join(wiki_root, "local", "domain", "wiki",
                           "acme-deploy-pipeline.md")
        import glob as _glob
        pers = _glob.glob(os.path.join(
            wiki_root, "local", "users", "*", "wiki",
            "acme-deploy-pipeline.md"))
        check("web", "wiki: librarian publishes domain + personal page",
              os.path.isfile(dom) and pers,
              (r.stdout[-120:], r.stderr[-120:]))
        ok = turn("Search the wiki for the ACME deploy rule and quote the "
                  "requirement.", ["staging"], tries=60)
        check("web", "wiki: agent finds the published rule via wiki_search", ok)
        page.screenshot(path=os.path.join(data, "wiki.png"))

        # ---- 4. NOTES + /compact -------------------------------------
        sid = new_session()
        ok = turn("Record this in your session notes with "
                  "update_session_notes: 'DECISION: the project codename is "
                  "otter-77.' Confirm when done.", ["otter-77"], tries=60)
        check("web", "notes: agent records via update_session_notes", ok)
        for i, w in enumerate(["alpha", "bravo", "charlie"]):
            turn(f"Reply with just the word: {w}", w, tries=45)
        settle(base, "alice", sid)
        box = page.locator("textarea.adk-composer-input")
        box.fill("/compact keep the codename decision, drop the filler words")
        page.wait_for_timeout(400)
        box.press("Enter")
        compacted = False
        for _ in range(100):
            page.wait_for_timeout(1000)
            if page.get_by_text("Compacted", exact=False).count() > 0:
                compacted = True
                break
        check("web", "notes: guided /compact completes", compacted)
        ok = turn("According to your session notes, what is the project "
                  "codename? Reply with just the codename.", "otter-77",
                  tries=60)
        check("web", "notes: codename survives compaction via notes", ok)
        page.screenshot(path=os.path.join(data, "notes-compact.png"))

        # ---- 5. MEMORY across sessions -------------------------------
        sid = new_session()
        turn("A durable fact about me you should remember: I only ever use "
             "pnpm, never npm or yarn. Acknowledge by repeating the package "
             "manager name.", ["pnpm"], tries=60)
        settle(base, "alice", sid, timeout_s=120)  # post-turn capture
        import glob
        epis = glob.glob(os.path.join(mem_root, "local", "users", "*",
                                      "episodic", "*.md"))
        check("web", "memory: capture wrote an episodic item", bool(epis),
              mem_root)
        new_session()
        # Keyword-fair phrasing: recall is keyword search (embedding recall
        # is the unshipped Fix E) — the query must share a term with the
        # stored note, or an honest miss is expected.
        ok = turn("Between pnpm, npm and yarn — which one do I use? Answer "
                  "from what you know about me, one word.", "pnpm", tries=60)
        check("web", "memory: fresh session recalls the fact", ok)
        page.screenshot(path=os.path.join(data, "memory.png"))
        browser.close()
    finally:
        _teardown(proc)
    return data


def run_desktop(pw):
    data = os.path.join(BATTERY, "desktop")
    # NOT "project": that basename text-collides with UI labels and the
    # row click then lands on the wrong element (first battery run).
    proj = os.path.join(BATTERY, "acme-notes-project")
    _mkdirs(data, proj)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)
    # Mirror the desktop launcher: wiki/memory hard-wired ON, per-project.
    proc, base = boot("desktop", 8963, data, {
        "ADK_CC_DESKTOP": "1",
        "ADK_CC_WIKI": "1", "ADK_CC_WIKI_ROOT": os.path.join(data, "wiki"),
        "ADK_CC_MEMORY": "1",
        "ADK_CC_MEMORY_ROOT": os.path.join(data, "memory"),
        "ADK_CC_KNOWLEDGE_UI": "1",
    })
    try:
        pid = requests.post(base + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        sid = "s-battery"
        sess = f"{base}/apps/adk_cc/users/{pid}/sessions/{sid}"
        requests.post(sess, json={}, timeout=30)
        requests.patch(sess, json={"state_delta": {
            "model_endpoint": "chatgpt-codex", "model_id": MODEL,
            "permission_mode": "bypassPermissions"}}, timeout=30)

        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 950})
        page.goto(base + "/", wait_until="networkidle")
        page.wait_for_timeout(1500)
        box = page.locator("textarea.adk-composer-input")
        for attempt in range(4):  # click through project → session, verify
            row = page.get_by_text(os.path.basename(proj), exact=False).first
            if row.count() > 0:
                row.click()
                page.wait_for_timeout(1500)
            srow = page.locator(".adk-session-row").first
            if srow.count() > 0:
                srow.click()
            page.wait_for_timeout(2000)
            if box.is_enabled():
                break
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1500)
        check("desktop", "ui: composer enabled after project/session select",
              box.is_enabled())
        turn = make_turn(page, base)

        # ---- 6. BUILD into the project dir (in-place workspace) -------
        ok = turn("Create a file notes/haiku.md in the project containing a "
                  "haiku about databases, then read it back to me.",
                  ["haiku"], tries=90)
        hk = os.path.join(proj, "notes", "haiku.md")
        check("desktop", "build: turn completes and reads the file back", ok)
        check("desktop", "build: file lands IN the project dir (in-place)",
              os.path.isfile(hk), hk)

        page.screenshot(path=os.path.join(data, "desktop-build.png"))
        browser.close()

        # ---- 7-9. extra left-behind sessions (API-driven real turns) ---
        def mk(sid_name):
            s = f"{base}/apps/adk_cc/users/{pid}/sessions/{sid_name}"
            requests.post(s, json={}, timeout=30)
            requests.patch(s, json={"state_delta": {
                "model_endpoint": "chatgpt-codex", "model_id": MODEL,
                "permission_mode": "bypassPermissions"}}, timeout=30)
            return sid_name

        s2 = mk("s-research")
        ok = (api_turn(base, pid, s2,
                       "In one paragraph, what is a write-ahead log and why "
                       "do databases use it?", "log")
              and api_turn(base, pid, s2,
                           "How does it interact with checkpointing? Briefly.",
                           "checkpoint")
              and api_turn(base, pid, s2,
                           "Summarize our whole conversation in one sentence.",
                           "log"))
        check("desktop", "research session: 3-turn WAL Q&A with recap", ok)

        s3 = mk("s-wiki")
        ok = api_turn(base, pid, s3,
                      "Use wiki_add to record — topic 'wal-checkpointing': "
                      "'Checkpoint frequency trades recovery time against "
                      "write amplification.' Confirm what you stored.",
                      "checkpoint")
        import glob as _g
        inbox = _g.glob(os.path.join(data, "wiki", "local", "users", "*",
                                     "inbox", "*wal*"))
        check("desktop", "wiki session: wiki_add lands in the inbox",
              ok and bool(inbox), inbox)

        s4 = mk("s-qna")
        ok = (api_turn(base, pid, s4,
                       "Pick one famous algorithm and name it. Name only.")
              and api_turn(base, pid, s4,
                           "Explain that SAME algorithm in two sentences, "
                           "repeating its name.")
              and api_turn(base, pid, s4,
                           "Which algorithm did you pick at the start? "
                           "Name only."))
        check("desktop", "qna session: 3-turn continuity flows", ok)
    finally:
        _teardown(proc)
    return data


def main() -> int:
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0
    if not os.path.isfile(os.path.expanduser(
            "~/.adk-cc-desktop/admin-data/model-endpoints.json")):
        print("SKIP: no model endpoint registry."); return 0
    for d in ("dist", "dist-desktop"):
        if not os.path.isfile(os.path.join(REPO, "web", d, "index.html")):
            print(f"SKIP: web/{d} not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    t0 = time.time()
    shells = os.environ.get("BATTERY_SHELL", "both")
    wdata = ddata = "-"
    with sync_playwright() as pw:
        if shells in ("both", "web"):
            wdata = run_web(pw)
        if shells in ("both", "desktop"):
            ddata = run_desktop(pw)
    mins = (time.time() - t0) / 60
    print(f"\n==== battery summary ({mins:.1f} min) ====")
    for line in _results:
        print("  " + line)
    print(f"\n{_passed} passed, {_failed} failed")
    print(f"artifacts: {wdata}  {ddata}")
    if KEEP_UP:
        print("servers LEFT RUNNING for inspection:")
        print("  web shell:     http://127.0.0.1:8961")
        print("  desktop shell: http://127.0.0.1:8963")
        print(f"relaunch later: {BATTERY}/serve-web.sh · "
              f"{BATTERY}/serve-desktop.sh")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
