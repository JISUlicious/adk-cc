"""Explore on an empty repository: does it stop, and does it stay in scope?

Both behaviours here were observed in live runs on a fresh `git init`:

  * A `**/*` glob returned only `.git` internals, and the next ten calls
    re-read `.git/HEAD`, re-listed `.git/refs/**` and grepped `.` across the
    tree to reach the conclusion the first call already gave.
  * It answered "I couldn't build the mobile shell app... in this read-only
    environment I also can't create the missing project files" — declining the
    coordinator's task and mistaking its own read-only rule for a property of
    the workspace. The coordinator then planned the work correctly, but that
    sentence was the last text in the thread.

Measured against the old prompt on this exact scenario, same model, by reverting
prompts.py in place and re-running: 11 Explore tool calls before, 1 after, and
the "names what is absent" check went FAIL → PASS.

The refusal check passed in BOTH runs, so this file does not demonstrate that
half of the fix — the refusal was observed once, verbatim, in an earlier live
run and is intermittent. That check is a regression guard, not evidence.

Thresholds are loose on purpose: the point is to catch a return to ten redundant
sweeps, not to pin a call count that varies run to run.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_explore_hygiene.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8969
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
_passed = _failed = 0

# Phrases that decline the caller's task or describe the workspace as read-only.
_REFUSAL_RE = re.compile(
    r"(?i)(i (?:couldn'?t|could not|can'?t|cannot) (?:build|create|implement|add)"
    r"|read-only environment"
    r"|nothing here to (?:extend|build)"
    r"|i (?:also )?can'?t create)"
)


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs a live model turn (ADK_CC_LIVE=1)."); return 0
    endpoints = os.path.expanduser(
        "~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(endpoints):
        print("SKIP: no model endpoint registry to borrow."); return 0

    data = tempfile.mkdtemp(prefix="explore-")
    proj = os.path.join(data, "project")
    os.makedirs(proj, exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)   # empty on purpose

    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_MODEL_REGISTRY_FILE": endpoints,
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
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
        sid = "explore1"
        sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
        requests.post(sess, json={}, timeout=30)
        requests.patch(sess, json={"state_delta": {
            "model_endpoint": "chatgpt-codex", "model_id": MODEL,
            "permission_mode": "plan"}}, timeout=30)   # plan: keep it to looking

        # The BUILD-shaped ask, not a neutral "look around". This matters: the
        # refusal prose appeared when the caller wanted an app built, which is
        # what tempts a search agent to judge feasibility. Measuring the polite
        # version would have scored a pass the failure never had.
        # Exploration-shaped so Explore is actually invoked, build-flavoured so
        # the temptation to judge feasibility is still there. A plain "build me
        # an app" had the coordinator do the work itself, leaving nothing to
        # measure; a plain "look around" removes the temptation entirely.
        prompt = ("Look around this project and tell me what is already here "
                  "that I could turn into a mobile app running a Linux shell.")
        t = requests.post(f"{BASE}/api/turns", timeout=60, json={
            "appName": "adk_cc", "userId": pid, "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text": prompt}]}}).json()
        for _ in range(120):
            time.sleep(3)
            st = requests.get(f"{BASE}/api/turns/{t['turn_id']}", timeout=30).json()
            if st["status"] != "running":
                break

        events = requests.get(sess, timeout=30).json()["events"]
        explore_calls, explore_text = 0, []
        for e in events:
            author = e.get("author") or ""
            for p in ((e.get("content") or {}).get("parts") or []):
                if author == "Explore" and p.get("functionCall"):
                    explore_calls += 1
                if author == "Explore" and p.get("text"):
                    explore_text.append(p["text"])
        blob = "\n".join(explore_text)

        if not explore_calls and not blob:
            print("    Explore was not used this turn — nothing to measure.")
            print(f"    artifacts: {data}")
            return 0

        print(f"    Explore made {explore_calls} tool call(s)")
        check("Explore stops once the tree is known to be empty",
              explore_calls <= 6,
              f"{explore_calls} calls to establish that only .git exists")
        hit = _REFUSAL_RE.search(blob)
        check("Explore reports findings instead of declining the task",
              hit is None,
              f"refusal prose: {hit.group(0)!r}" if hit else "")
        check("Explore names what is absent",
              bool(re.search(r"(?i)(\.git|no (package\.json|src|scaffold|source))", blob)),
              "no statement of what the tree does or does not contain")
        with open(os.path.join(data, "explore.json"), "w") as fh:
            json.dump({"calls": explore_calls, "text": blob}, fh, indent=2)
        print(f"    artifacts: {data}")
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
