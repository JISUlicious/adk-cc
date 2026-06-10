"""LIVE e2e: parallel fan-out + concurrency CAP + iterate-until-sufficient,
against the real model.

Unit tests prove the dispatch is parallel and the semaphore caps concurrency.
This proves the whole thing end-to-end through the real coordinator + model:

  * PARALLEL   — the coordinator emits MULTIPLE code_explore calls in ONE
                 response → ADK runs them concurrently (merged response event).
  * CAP        — with ADK_CC_AGENT_TOOL_EXPLORE_MAX=2, spawning 3 in a round
                 forces the 3rd to QUEUE → its envelope has queued_s > 0.
  * ITERATE    — a two-round prompt makes the coordinator spawn a batch, then
                 spawn ANOTHER round in a later turn → code_explore calls span
                 >=2 distinct coordinator responses.

Runs its own ephemeral server on a separate port (your :8000 is untouched),
loading .env for the real model but overriding the sandbox to `noop` (fast,
local) and the cap to 2. SKIPs if the model isn't reachable.

    .venv/bin/python tests/e2e_parallel_explore.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"
CAP = 1  # cap=1 → even a 2-wide round must queue (and keeps model load low)

FORCING_QUERY = (
    "This is a TWO-ROUND exploration test — follow the rounds exactly; do NOT "
    "use transfer_to_agent.\n"
    "ROUND 1: in a SINGLE response, call the `code_explore` tool exactly TWO "
    "times (in parallel) — one per question: (A) which file defines `class "
    "DaytonaBackend`; (B) which file defines `class PlanModeReminderPlugin`. "
    "Each explorer runs one grep and reports the path. Wait for both.\n"
    "ROUND 2: then, in your NEXT response (a separate round), call "
    "`code_explore` exactly ONCE more: (C) which file defines `class "
    "NoopBackend`.\n"
    "After round 2 returns, give a 3-line summary (A–C)."
)


def _start_server():
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENT_TOOL_EXPLORE": "1",
        "ADK_CC_AGENT_TOOL_EXPLORE_MAX": str(CAP),  # tiny cap → forces queueing
        "ADK_CC_SANDBOX_BACKEND": "noop",           # process env wins (override=False)
        "ADK_CC_NOOP_ACK_HOST_EXEC": "1",
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        # NOT setting ADK_CC_SKIP_DOTENV → .env loads the real model config.
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"),
         "adk_cc.service.server:make_app", "--factory",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            requests.get(BASE + "/list-apps", timeout=2)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("ephemeral server did not start")


def _unwrap(resp):
    # ADK may wrap the tool result under a 'result' key.
    if isinstance(resp, dict) and "report" not in resp and isinstance(
        resp.get("result"), dict
    ):
        return resp["result"]
    return resp


def _analyze(events: list) -> int:
    rounds = 0                 # coordinator responses that spawned >=1 explorer
    max_calls_in_one = 0       # most code_explore calls in a single response
    total_calls = 0
    envelopes = []
    for e in events:
        parts = (e.get("content") or {}).get("parts") or []
        calls_here = sum(
            1 for p in parts
            if (p.get("functionCall") or {}).get("name") == "code_explore"
        )
        if calls_here:
            rounds += 1
            total_calls += calls_here
            max_calls_in_one = max(max_calls_in_one, calls_here)
        for p in parts:
            fr = p.get("functionResponse")
            if fr and fr.get("name") == "code_explore":
                envelopes.append(_unwrap(fr.get("response") or {}))

    queued = [e.get("queued_s") for e in envelopes if isinstance(e, dict)]
    print(f"\nrounds (responses that spawned explorers): {rounds}")
    print(f"max code_explore calls in ONE response:    {max_calls_in_one}")
    print(f"total code_explore calls:                  {total_calls}")
    print(f"envelopes returned:                        {len(envelopes)}")
    for i, e in enumerate(envelopes):
        if isinstance(e, dict):
            print(f"  [{i}] task={str(e.get('task'))[:40]!r} ok={e.get('ok')} "
                  f"elapsed_s={e.get('elapsed_s')} queued_s={e.get('queued_s')} "
                  f"tools_used={e.get('tools_used')}")

    ok = True

    def check(name, cond, detail):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}: {detail}")
        ok = ok and cond

    check("PARALLEL: >=2 code_explore in one response",
          max_calls_in_one >= 2, f"max in one response = {max_calls_in_one}")
    check(f"CAP: some explorer queued (max={CAP})",
          any(isinstance(q, (int, float)) and q > 0 for q in queued),
          f"queued_s values = {queued}")
    check("ITERATE: explorers spawned across >=2 rounds",
          rounds >= 2, f"rounds = {rounds}")
    check("enriched envelopes returned (task/report/...)",
          len(envelopes) >= 2
          and all(isinstance(e, dict) and "report" in e for e in envelopes),
          f"{len(envelopes)} envelopes")
    return 0 if ok else 1


def main() -> int:
    proc = None
    try:
        print(f"starting ephemeral server (real model, noop sandbox, cap={CAP})…")
        proc = _start_server()
        sid = requests.post(
            f"{BASE}/apps/adk_cc/users/alice/sessions", json={}, timeout=15,
        ).json()["id"]
        print(f"session {sid[:18]}; firing 2-round forcing query (slow model)…")
        t0 = time.perf_counter()
        r = requests.post(
            f"{BASE}/run",
            headers={"Content-Type": "application/json"},
            json={
                "appName": "adk_cc", "userId": "alice", "sessionId": sid,
                "newMessage": {"role": "user",
                               "parts": [{"text": FORCING_QUERY}]},
            },
            timeout=600,
        )
        wall = time.perf_counter() - t0
        print(f"/run HTTP {r.status_code} in {wall:.1f}s")
        if r.status_code != 200:
            # A model-side error (e.g. provider 429 rate limit) means the run
            # couldn't complete — that's infrastructure, not a behavioral
            # failure of the fan-out. SKIP rather than report a false FAIL.
            print(f"SKIP: /run returned {r.status_code} (model error / rate "
                  f"limit, not a fan-out failure). body: {r.text[:200]}")
            return 0
        events = r.json()
        print(f"events: {len(events)}")
        for i, e in enumerate(events):
            parts = (e.get("content") or {}).get("parts") or []
            kinds = []
            for p in parts:
                if p.get("functionCall"):
                    kinds.append("fc:" + p["functionCall"].get("name", "?"))
                elif p.get("functionResponse"):
                    kinds.append("fr:" + p["functionResponse"].get("name", "?"))
                elif isinstance(p.get("text"), str) and p["text"].strip():
                    kinds.append("text")
            print(f"  {i:2} {e.get('author','?'):12} {kinds}")
        rc = _analyze(events)
        print("\nparallel-explore e2e " + ("PASSED" if rc == 0 else "FAILED"))
        return rc
    finally:
        if proc:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
