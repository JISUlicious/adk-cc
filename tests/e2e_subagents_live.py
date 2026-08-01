"""Live: spawn/collect explorers on a real multi-question task, UI included.

What only a live run can show:
  * the model actually chooses spawn_explorers for a naturally-parallel ask,
  * the fan-out is genuinely parallel (wall-clock < sum of per-child elapsed),
  * results are attributable (each report echoes its task),
  * the thread SHOWS the spawned agents (the user's requirement) — the spawn
    card and the collect card render, never folded.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_subagents_live.py
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8957
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + str(detail)) if detail and not ok else ''}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0
    endpoints = os.path.expanduser(
        "~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(endpoints):
        print("SKIP: no model endpoint registry."); return 0
    dist = os.path.join(REPO, "web", "dist-desktop")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print("SKIP: web UI not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    data = tempfile.mkdtemp(prefix="subag-")
    # The project under exploration is THIS repo's agents tree — real code,
    # several genuinely independent questions.
    proj = os.path.join(REPO)

    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_MODEL_REGISTRY_FILE": endpoints,
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SERVE_UI": "1", "ADK_CC_UI_DIST": dist,
        "ADK_CC_SUBAGENTS": "1",
        "ADK_CC_TRUST_PROJECT_SKILLS": "1",
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env,
        stdout=open(os.path.join(data, "server.log"), "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)

        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        sid = "s-subag"
        sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
        requests.post(sess, json={}, timeout=30)
        requests.patch(sess, json={"state_delta": {
            "model_endpoint": "chatgpt-codex", "model_id": MODEL}}, timeout=30)

        t = requests.post(f"{BASE}/api/turns", timeout=60, json={
            "appName": "adk_cc", "userId": pid, "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text":
                "Answer three independent questions about this repo, in "
                "parallel if you can: (1) which permission modes exist and "
                "where are they defined; (2) which sandbox backends exist; "
                "(3) how are skills discovered — which directories, what "
                "order. Then give me one combined summary."}]}}).json()
        t0 = time.time()
        dock_seen = []
        for _ in range(200):
            time.sleep(3)
            st = requests.get(f"{BASE}/api/turns/{t['turn_id']}", timeout=30).json()
            # The right-panel dock's data source, probed while the run is hot:
            # children exist only between spawn and collect.
            try:
                snap = requests.get(
                    f"{BASE}/api/subagents?app_name=adk_cc&user_id={pid}"
                    f"&session_id={sid}", timeout=10).json()
                if snap.get("children"):
                    dock_seen.append(snap["children"])
            except Exception:
                pass
            if st["status"] != "running":
                break
        wall = time.time() - t0
        print(f"    turn: {st.get('status')} in ~{wall:.0f}s")

        events = requests.get(sess, timeout=30).json()["events"]
        spawn_calls, collects, answers = [], [], []
        for e in events:
            for p in ((e.get("content") or {}).get("parts") or []):
                fc = p.get("functionCall") or {}
                fr = p.get("functionResponse") or {}
                if fc.get("name") == "spawn_explorers":
                    spawn_calls.append(fc.get("args") or {})
                if fr.get("name") == "collect_explorers":
                    collects.append(fr.get("response") or {})
                if (p.get("text") and not p.get("thought")
                        and e.get("author") == "coordinator"):
                    answers.append(" ".join(p["text"].split()))

        check("the model chose to spawn explorers", bool(spawn_calls),
              "no spawn_explorers call")
        tasks = [t for s in spawn_calls for t in (s.get("tasks") or [])]
        print(f"    spawned: {len(tasks)} tasks")
        done = [d for c in collects for d in (c.get("done") or [])]
        check("collect returned their reports", len(done) >= 2,
              f"{len(done)} reports")
        check("reports are attributable to their tasks",
              all(d.get("task") and d.get("id") for d in done),
              done[:1])
        oks = [d for d in done if d.get("ok")]
        check("explorers actually explored (tool calls > 0)",
              all(d.get("tool_calls", 0) > 0 for d in oks),
              [(d.get("task"), d.get("tool_calls")) for d in done])
        seq = sum(float(d.get("elapsed_s") or 0) for d in oks)
        # Parallelism, measured where it actually shows: the window between
        # the spawn call and the first collect RESPONSE (event timestamps).
        # Serial children would stretch that window to at least their sum;
        # parallel ones compress it toward the slowest child. The first
        # version compared the WHOLE TURN's wall clock — which mostly
        # measures the model's own thinking, not the fan-out.
        t_spawn = t_collect = None
        for e in events:
            ts = e.get("timestamp")
            for p in ((e.get("content") or {}).get("parts") or []):
                if (p.get("functionCall") or {}).get("name") == "spawn_explorers" \
                        and t_spawn is None:
                    t_spawn = ts
                if (p.get("functionResponse") or {}).get("name") == "collect_explorers" \
                        and t_collect is None:
                    t_collect = ts
        window = (t_collect - t_spawn) if (t_spawn and t_collect) else None
        check("the fan-out ran in parallel (spawn→collect window < sum of work)",
              window is not None and len(oks) >= 2 and window < seq - 5,
              f"window={window and round(window)}s vs sum={seq:.0f}s")
        check("the dock endpoint saw them running mid-turn",
              any(any(c.get("status") == "running" for c in snap)
                  for snap in dock_seen),
              f"{len(dock_seen)} snapshots")
        check("a combined answer followed", bool(answers) and len(answers[-1]) > 100,
              answers[-1][:80] if answers else "none")

        # ---- the UI shows it -------------------------------------------
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            # The project shares its name with the app wordmark, so a bare
            # text match clicks the LOGO (measured: blank thread, 0 cards).
            # The rail rows have chevrons; click the row inside the projects
            # list specifically.
            row = page.locator("aside, nav, div").filter(
                has_text="Projects").locator(
                f"text={os.path.basename(proj)}").nth(1)
            try:
                row.click(timeout=5000)
            except Exception:
                page.get_by_text(os.path.basename(proj),
                                 exact=False).nth(1).click()
            page.wait_for_timeout(1000)
            srow = page.locator(".adk-session-row").first
            if srow.count():
                srow.click(); page.wait_for_timeout(2500)
            cards = page.locator("[data-agent-card]")
            check("the thread shows the spawned agents as their own cards",
                  cards.count() >= 2, f"{cards.count()} cards")
            text = " ".join(
                cards.nth(i).inner_text() for i in range(cards.count()))
            check("the cards say what ran",
                  "explorer" in text.lower() or "report" in text.lower(),
                  text[:120])
            if cards.count():
                cards.first.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
            page.screenshot(path=os.path.join(data, "subagents.png"))
            b.close()
        print(f"    screenshot: {data}/subagents.png")
        with open(os.path.join(data, "summary.json"), "w") as fh:
            json.dump({"tasks": tasks, "done": done, "wall_s": wall}, fh, indent=2)
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
