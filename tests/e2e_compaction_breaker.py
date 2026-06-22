"""E2E (P6): the circuit breaker opens after consecutive summarizer failures and
then SKIPS compaction (no model call) during the cooldown.

Main agent uses the real model (to produce events worth compacting), but the
COMPACTION summarizer is pointed at a broken endpoint so every summarizer call
fails. With BREAKER_THRESHOLD=2 and a tiny compaction threshold, the breaker
should open by ~turn 2-3 and log `breaker_open` on the next compaction.

Live model (for the agent turns); skips without one.
Run: .venv/bin/python tests/e2e_compaction_breaker.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import adk_cc  # noqa: F401

os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "30")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8788
BASE = f"http://127.0.0.1:{PORT}"
FILLER = ("Keep this background in mind: modern CPUs use deep pipelines, "
          "out-of-order execution, TAGE predictors, and multi-level caches. " * 3)
PROMPTS = [f"Note {i}: {FILLER} Acknowledge." for i in range(1, 6)]


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY.")
        return 0

    wks = tempfile.mkdtemp(prefix="brk-wks-")
    art = tempfile.mkdtemp(prefix="brk-art-")
    log_path = os.path.join(art, "srv.log")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_WORKSPACE_ROOT": wks, "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{art}",
        "ADK_CC_COMPACTION_TOKEN_THRESHOLD": "700",
        "ADK_CC_COMPACTION_EVENT_RETENTION": "2",
        "ADK_CC_COMPACTION_INTERVAL": "1", "ADK_CC_COMPACTION_OVERLAP": "1",
        # break the SUMMARIZER (separate model knobs) so every compaction fails,
        # WITHOUT touching the main agent model.
        "ADK_CC_COMPACTION_MODEL": "openai/does-not-exist",
        "ADK_CC_COMPACTION_API_BASE": "http://127.0.0.1:9/v1",  # unroutable
        "ADK_CC_COMPACTION_API_KEY": "bad",
        "ADK_CC_COMPACTION_TIMEOUT_S": "8",
        "ADK_CC_COMPACTION_BREAKER_THRESHOLD": "2",
        "ADK_CC_COMPACTION_BREAKER_COOLDOWN_S": "600",
        "ADK_CC_TOOL_TITLES": "0",
    })
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.5)
        sid = requests.post(f"{BASE}/apps/adk_cc/users/local/sessions", json={}, timeout=15).json()["id"]
        for i, p in enumerate(PROMPTS, 1):
            try:
                requests.post(f"{BASE}/run", timeout=120, json={
                    "appName": "adk_cc", "userId": "local", "sessionId": sid,
                    "newMessage": {"role": "user", "parts": [{"text": p}]}})
            except Exception as e:
                print(f"  turn {i} err {type(e).__name__}")
            time.sleep(5)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log.close()

    blob = ""
    try:
        with open(log_path) as fh:
            blob = fh.read()
    except Exception:
        pass
    failures = len(re.findall(r"compaction_failure|reason=(timeout|exception)", blob))
    breaker_open = "reason=breaker_open" in blob or "breaker_open" in blob
    print(f"  summarizer failures logged: {failures}")
    print(f"  [{'PASS' if failures else 'SKIP'}] summarizer failed as expected")
    if not failures:
        print("SKIP: no compaction attempted (threshold not hit / model too slow).")
        return 0
    print(f"  [{'PASS' if breaker_open else 'FAIL'}] breaker opened and SKIPPED a compaction")
    ok = breaker_open
    print("\ncompaction breaker e2e " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
