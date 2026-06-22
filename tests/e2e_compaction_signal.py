"""E2E: session-context compaction emits the signal the UI renders.

Boots a server with a TINY compaction threshold, drives enough turns to cross
it, then asserts the session event stream carries `actions.compaction` with a
`compactedContent` summary — the exact field Thread.tsx/CompactionDivider read.
This proves the P1 indicator's data path end to end (ADK serializes the same
way for REST and /run_sse).

Live model required (the summarizer is an LLM call); skips gracefully without
one. Paced for the rate limit. Run:
    .venv/bin/python tests/e2e_compaction_signal.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

import adk_cc  # noqa: F401 — bootstraps .env (model creds)

os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "30")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8778
BASE = f"http://127.0.0.1:{PORT}"
APP = "adk_cc"
USER = "local"
# A chunky paragraph (~250-300 tokens) so a few turns blow past the threshold.
FILLER = (
    "Please carefully consider the following background context about CPU "
    "microarchitecture and keep it in mind for the rest of our conversation. "
    "Modern high-performance cores use deep pipelines, out-of-order execution "
    "with large reorder buffers, sophisticated branch predictors such as TAGE, "
    "multi-level cache hierarchies, simultaneous multithreading, wide "
    "superscalar decode, physical register files with hundreds of entries, and "
    "aggressive prefetching. " * 4
)
PROMPTS = [f"Note {i}: {FILLER} Briefly acknowledge." for i in range(1, 9)]


def _find_compaction(events) -> dict | None:
    """Scan session events for an actions.compaction payload (camelCase or
    snake_case), returning the first one found."""
    for e in events or []:
        actions = e.get("actions") or {}
        comp = actions.get("compaction") or actions.get("Compaction")
        if isinstance(comp, dict):
            return comp
    return None


def _summary_text(comp: dict) -> str:
    content = comp.get("compactedContent") or comp.get("compacted_content") or {}
    parts = content.get("parts") or []
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY — compaction e2e skipped.")
        return 0

    wks = tempfile.mkdtemp(prefix="comp-wks-")
    data_dir = tempfile.mkdtemp(prefix="comp-art-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_WORKSPACE_ROOT": wks,
        "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{data_dir}",
        # tiny compaction config — threshold + retention must be set together
        "ADK_CC_COMPACTION_TOKEN_THRESHOLD": "800",
        "ADK_CC_COMPACTION_EVENT_RETENTION": "3",
        "ADK_CC_COMPACTION_INTERVAL": "1",
        "ADK_CC_COMPACTION_OVERLAP": "1",
        # keep the guard out of the way (don't reject before we can compact)
        "ADK_CC_MAX_CONTEXT_TOKENS": "200000",
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

        sid = requests.post(f"{BASE}/apps/{APP}/users/{USER}/sessions",
                            json={}, timeout=15).json()["id"]
        sess_url = f"{BASE}/apps/{APP}/users/{USER}/sessions/{sid}"

        try:
            requests.post(f"{BASE}/run", timeout=60, json={
                "appName": APP, "userId": USER, "sessionId": sid,
                "newMessage": {"role": "user", "parts": [{"text": "say ok"}]}})
        except Exception as e:
            print(f"SKIP: model unreachable ({type(e).__name__}).")
            return 0

        comp = None
        sent = 0
        for prompt in PROMPTS:
            try:
                requests.post(f"{BASE}/run", timeout=120, json={
                    "appName": APP, "userId": USER, "sessionId": sid,
                    "newMessage": {"role": "user", "parts": [{"text": prompt}]}})
                sent += 1
            except requests.RequestException as e:
                print(f"  turn errored ({type(e).__name__}); continuing")
                time.sleep(8)
                continue
            events = requests.get(sess_url, timeout=15).json().get("events", [])
            comp = _find_compaction(events)
            print(f"  turn {sent}: events={len(events)} compaction={'YES' if comp else 'no'}")
            if comp:
                break
            time.sleep(6)  # pace

        if sent == 0:
            print("SKIP: no turn completed (model too slow).")
            return 0

        ok = comp is not None
        if ok:
            summary = _summary_text(comp)
            print(f"\n  [PASS] actions.compaction present after {sent} turn(s)")
            print(f"  [{'PASS' if summary else 'WARN'}] compactedContent summary "
                  f"({len(summary)} chars): {summary[:160]!r}")
            print(f"  [info] startTimestamp={comp.get('startTimestamp')} "
                  f"endTimestamp={comp.get('endTimestamp')}")
            ok = ok and bool(summary)
        else:
            print(f"\n  [FAIL] no actions.compaction after {sent} turn(s) — "
                  "threshold not crossed or compaction didn't fire.")

        print("\ncompaction signal e2e " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        proc.kill()
        for d in (wks, data_dir):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
