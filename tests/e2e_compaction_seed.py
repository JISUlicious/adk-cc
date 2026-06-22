"""E2E (P3): with ADK_CC_COMPACTION_SEED_MEMORY=1, the compaction summary is
seeded with the user's durable memory. Seed a semantic fact, drive turns past a
tiny threshold, assert the fact appears in the compactedContent. Live model;
skips without one.

Run: .venv/bin/python tests/e2e_compaction_seed.py
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
PORT = 8786
BASE = f"http://127.0.0.1:{PORT}"
FACT = "RAINTANK-9000"  # a distinctive durable fact unlikely to appear by chance
PROMPTS = [
    "My project's internal code name is RAINTANK-9000. Acknowledge briefly.",
    "Here is some background to keep in mind: modern CPUs use deep pipelines, "
    "out-of-order execution, TAGE branch predictors, and multi-level caches. " * 3
    + " Acknowledge.",
    "What is my project's code name?",
]


def _find_comp(events):
    for e in events or []:
        c = (e.get("actions") or {}).get("compaction")
        if isinstance(c, dict):
            return c
    return None


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY — seed e2e skipped.")
        return 0

    mem_root = tempfile.mkdtemp(prefix="seed-mem-")
    wks = tempfile.mkdtemp(prefix="seed-wks-")
    art = tempfile.mkdtemp(prefix="seed-art-")

    # Pre-seed a durable SEMANTIC fact so recall finds it (model-free).
    from adk_cc.memory import MemoryStore, consolidate_user
    s = MemoryStore.for_tenant("local", root=mem_root)
    for _ in range(2):
        s.add_episodic("local", f"The project's internal code name is {FACT}.", topic="code-name")
    consolidate_user(s, "local")

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_MEMORY": "1",
        "ADK_CC_MEMORY_ROOT": mem_root,
        "ADK_CC_WORKSPACE_ROOT": wks,
        "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{art}",
        "ADK_CC_COMPACTION_SEED_MEMORY": "1",
        "ADK_CC_COMPACTION_TOKEN_THRESHOLD": "700",
        "ADK_CC_COMPACTION_EVENT_RETENTION": "2",
        "ADK_CC_COMPACTION_INTERVAL": "1",
        "ADK_CC_COMPACTION_OVERLAP": "1",
        "ADK_CC_TOOL_TITLES": "0",
        # avoid the autocapture LLM call adding noise/cost during the probe
        "ADK_CC_MEMORY_AUTOCAPTURE": "0",
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
                print(f"  turn {i} err: {type(e).__name__}")
                time.sleep(8)
                continue
            comp = _find_comp(requests.get(surl, timeout=15).json().get("events", []))
            print(f"  after turn {i}: compaction={'YES' if comp else 'no'}")
            if comp:
                break
            time.sleep(5)

        if not comp:
            print("SKIP: no compaction captured (model too slow / threshold not hit).")
            return 0

        content = comp.get("compactedContent") or {}
        txt = "\n".join(p.get("text", "") for p in content.get("parts") or [])
        seeded = "Durable facts about this user" in txt
        has_fact = FACT in txt
        print(f"\n  summary[:240]: {txt[:240]!r}")
        print(f"  [{'PASS' if seeded else 'FAIL'}] summary seeded with memory preamble")
        print(f"  [{'PASS' if has_fact else 'FAIL'}] seeded the durable fact ({FACT})")
        ok = seeded and has_fact
        print("\ncompaction seed e2e " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        proc.kill()
        for d in (mem_root, wks, art):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
