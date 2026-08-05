"""The #114 fix, proven in the running product.

The whole lesson of #114 is that in-process green did not mean production
green: the confirmation machinery passed every isolated test while every real
confirmation resume silently died, because only production runs the resumable
App through the Turn Broker against file-backed sessions. So the fix cannot
be trusted from the unit matrix alone — this drives the REAL server with a
REAL model: trigger a gated run_bash, answer the confirmation exactly as the
UI does, and require the command to actually execute.

Asserts the three things the live failure showed:
  1. the answered confirmation RESUMES the tool (its real output appears),
  2. no "Continue." auto-continue is injected after the answer,
  3. the model's reply reflects the executed command, not "still waiting".

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_confirmation_resume_live.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8981
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:  # noqa: PLR0915
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs a live model (ADK_CC_LIVE=1)."); return 0

    data = tempfile.mkdtemp(prefix="confres-data-")
    proj = tempfile.mkdtemp(prefix="confres-proj-")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=False)
    # The gated action: a shell-rc write — the scenario the permission e2e
    # already proved raises a card live (a model will readily attempt it,
    # unlike `rm -rf`, which it refused outright when this test tried that).
    # $HOME is redirected so the .zshrc is a fixture, not the operator's.
    home = tempfile.mkdtemp(prefix="confres-home-")
    zshrc = os.path.join(home, ".zshrc")
    open(zshrc, "w").write("# fixture\n")
    for rel in (".codex", ".adk-cc-desktop"):
        src = os.path.expanduser(f"~/{rel}")
        if os.path.exists(src):
            try:
                os.symlink(src, os.path.join(home, rel))
            except OSError:
                pass

    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    env.update({
        "ADK_CC_MODEL_REGISTRY_FILE": os.path.expanduser(
            "~/.adk-cc-desktop/admin-data/model-endpoints.json"),
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "HOME": home,
    })
    log = os.path.join(data, "server.log")
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=open(log, "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(160):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        sid = "confres-1"
        sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
        requests.post(sess, json={}, timeout=30)
        requests.patch(sess, json={"state_delta": {
            "model_endpoint": "chatgpt-codex", "model_id": MODEL}}, timeout=30)

        # A dangerous-classified command → the gate must ask.
        t = requests.post(f"{BASE}/api/turns", timeout=60, json={
            "appName": "adk_cc", "userId": pid, "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text":
                "Use run_bash to append the line `export CONF_TEST=1` to my "
                "~/.zshrc, then confirm it is there."}]}}).json()
        for _ in range(60):
            time.sleep(3)
            st = requests.get(f"{BASE}/api/turns/{t['turn_id']}", timeout=30).json()
            if st["status"] != "running":
                break

        events = requests.get(sess, timeout=30).json().get("events") or []
        wraps = []
        for e in events:
            for p in (e.get("content") or {}).get("parts") or []:
                fc = p.get("functionCall")
                if fc and fc.get("name") in ("adk_cc_confirmation_form",
                                             "adk_request_confirmation"):
                    wraps.append(fc["id"])
        check("the command was gated (confirmation card raised)",
              len(wraps) >= 1, f"{len(wraps)} wraps; see {log}")
        if not wraps:
            return 1
        check("the write is parked, not done (gate really blocked it)",
              "CONF_TEST" not in open(zshrc).read())

        # Answer every card EXACTLY as the UI does: functionResponse with the
        # FORM name and {chose_id}, one turn per card, via /api/turns.
        for wid in wraps:
            t2 = requests.post(f"{BASE}/api/turns", timeout=120, json={
                "appName": "adk_cc", "userId": pid, "sessionId": sid,
                "newMessage": {"role": "user", "parts": [{
                    "functionResponse": {
                        "id": wid, "name": "adk_cc_confirmation_form",
                        "response": {"chose_id": "allow_once"}}}]}}).json()
            for _ in range(60):
                time.sleep(3)
                st2 = requests.get(f"{BASE}/api/turns/{t2['turn_id']}",
                                   timeout=30).json()
                if st2["status"] != "running":
                    break

        events2 = requests.get(sess, timeout=30).json().get("events") or []
        blob = json.dumps(events2)
        done = "CONF_TEST" in open(zshrc).read()
        check("the tool RESUMED after the answer (the write actually landed)",
              done, open(zshrc).read()[:120])
        # The signature of the bug: a broker auto-continue after the answer.
        n_continue = blob.count('"Continue."')
        check("no 'Continue.' was injected after the answer", n_continue == 0,
              f"{n_continue} auto-continues")
        tail_text = " ".join(
            p.get("text", "") for e in events2[-6:]
            for p in (e.get("content") or {}).get("parts") or [])
        check("the model's reply is about the result, not 'still waiting'",
              "waiting" not in tail_text.lower() or "CONF_TEST" in tail_text,
              tail_text[:200])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n{_passed} passed, {_failed} failed")
    if _failed:
        print(f"server log: {log}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
