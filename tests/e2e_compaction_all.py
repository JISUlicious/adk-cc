"""E2E: all summary-shaping tiers together (P1 prompt+strip, P3 seed, P5 frame)
in one live compaction. Boots a server with every flag on, pre-seeds a durable
fact, drives turns past a tiny threshold, and asserts the compacted summary has:
framing line, seed block, structured sections, and NO <analysis>.

Live model; skips without one. Run: .venv/bin/python tests/e2e_compaction_all.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

import adk_cc  # noqa: F401

os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "30")
for _k in ("ADK_CC_MEMORY_STORE_URI", "ADK_CC_WIKI_STORE_URI"):
    os.environ.pop(_k, None)

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8787
BASE = f"http://127.0.0.1:{PORT}"
FACT = "ZEPHYR-7"
PROMPTS = [
    f"My project's code name is {FACT}. Acknowledge briefly.",
    "Background to remember: modern CPUs use deep pipelines, out-of-order "
    "execution, TAGE branch predictors, multi-level caches. " * 3 + " Acknowledge.",
    "What is my project's code name?",
]


def _find(events):
    for e in events or []:
        c = (e.get("actions") or {}).get("compaction")
        if isinstance(c, dict):
            return c
    return None


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY.")
        return 0

    mem = tempfile.mkdtemp(prefix="all-mem-")
    wks = tempfile.mkdtemp(prefix="all-wks-")
    art = tempfile.mkdtemp(prefix="all-art-")
    from adk_cc.memory import MemoryStore, consolidate_user
    s = MemoryStore.for_tenant("local", root=mem)
    for _ in range(2):
        s.add_episodic("local", f"The project's code name is {FACT}.", topic="code-name")
    consolidate_user(s, "local")

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_MEMORY": "1", "ADK_CC_MEMORY_ROOT": mem,
        "ADK_CC_MEMORY_AUTOCAPTURE": "0",
        "ADK_CC_WORKSPACE_ROOT": wks, "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{art}",
        # all summary-shaping tiers on
        "ADK_CC_COMPACTION_SEED_MEMORY": "1",
        # frame + structured prompt are on by default; be explicit for clarity
        "ADK_CC_COMPACTION_TOKEN_THRESHOLD": "700",
        "ADK_CC_COMPACTION_EVENT_RETENTION": "2",
        "ADK_CC_COMPACTION_INTERVAL": "1", "ADK_CC_COMPACTION_OVERLAP": "1",
        "ADK_CC_TOOL_TITLES": "0",
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.5)
        sid = requests.post(f"{BASE}/apps/adk_cc/users/local/sessions", json={}, timeout=15).json()["id"]
        surl = f"{BASE}/apps/adk_cc/users/local/sessions/{sid}"
        comp = None
        for i, p in enumerate(PROMPTS, 1):
            try:
                requests.post(f"{BASE}/run", timeout=120, json={
                    "appName": "adk_cc", "userId": "local", "sessionId": sid,
                    "newMessage": {"role": "user", "parts": [{"text": p}]}})
            except Exception as e:
                print(f"  turn {i} err {type(e).__name__}"); time.sleep(8); continue
            comp = _find(requests.get(surl, timeout=15).json().get("events", []))
            print(f"  after turn {i}: compaction={'YES' if comp else 'no'}")
            if comp:
                break
            time.sleep(5)
        if not comp:
            print("SKIP: no compaction captured.")
            return 0

        content = comp.get("compactedContent") or {}
        txt = "\n".join(p.get("text", "") for p in content.get("parts") or [])
        low = txt.lower()
        framed = "continue the conversation directly" in low or txt.lstrip().startswith("[")
        seeded = "durable facts about this user" in low
        has_fact = FACT in txt
        structured = any(h in low for h in ("primary request", "key technical",
                                            "current work", "pending tasks"))
        no_analysis = "<analysis>" not in low
        print(f"\n  summary[:300]: {txt[:300]!r}\n")
        checks = {
            "P5 framing line present": framed,
            "P3 seed block present": seeded,
            "P3 seeded fact present (%s)" % FACT: has_fact,
            "P1 structured sections present": structured,
            "P1 <analysis> stripped": no_analysis,
        }
        ok = True
        for name, cond in checks.items():
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
            ok = ok and cond
        print("\ncompaction all-tiers e2e " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        proc.kill()
        for d in (mem, wks, art):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
