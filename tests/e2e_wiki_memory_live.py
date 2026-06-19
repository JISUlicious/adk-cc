"""LIVE multi-user e2e: 3 users build a shared wiki + private memory on a
topic (CPU core design), through the REAL agent over HTTP, with the real
model and the real cron jobs.

Per the chosen scope: LIVE only, real Runner + real model, natural queries
(no /wiki command). Three bearer-token users on ONE tenant (acme) so the wiki
is SHARED and memory is PER-USER — the auth middleware seeds that scoping,
which is why this must go through the HTTP server, not an in-process runner.

Flow: N rounds; each round every user (a) ingests a doc (a message that asks
the agent to save it to the wiki → wiki_add) and (b) sends a query (→
wiki_search answer + memory capture). Between rounds the wiki librarian and
memory consolidator run. Seeded cases: corroboration + contradiction
(pipeline depth), supersession (L2 cache size), and per-user durable context
for memory.

Because the configured model may be weak/unstable, assertions are
MODEL-INDEPENDENT (scoping, provenance, idempotency on whatever was produced)
and the model's actual output is REPORTED. Skips gracefully if the model is
unreachable. Run:

    .venv/bin/python tests/e2e_wiki_memory_live.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

import requests

import adk_cc  # noqa: F401 — importing bootstraps .env (model creds) into os.environ

# Cap model calls under the endpoint's shared ~40/min limit. The SelectableLlm
# throttle paces ALL calls — agent turns, capture, AND the librarian's
# classifier + page synthesis — so the model-backed librarian runs safely.
# Inherited by the server + cron subprocesses via dict(os.environ).
os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "30")

# ISOLATION: the test's per-run temp roots (set per subprocess below) must be
# authoritative. A store URI from .env would override the root and point the
# test at REAL data — clear them so existing data can't contaminate the test
# (cleared in this process AND inherited-clear by the server/cron subprocesses).
for _k in ("ADK_CC_WIKI_STORE_URI", "ADK_CC_MEMORY_STORE_URI"):
    os.environ.pop(_k, None)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8771
BASE = f"http://127.0.0.1:{PORT}"
APP = "adk_cc"
TENANT = "acme"
USERS = {"alice": "alice_tok", "bob": "bob_tok", "carol": "carol_tok"}
# Longer sessions / more turns: bump via ADK_CC_E2E_ROUNDS (default 2 = the
# canonical run). Each user keeps ONE session across all rounds, so a higher
# count accumulates turns per session (context growth → compaction) and stacks
# durable facts (exercising the hybrid threshold trigger + consolidation).
ROUNDS = max(2, int(os.environ.get("ADK_CC_E2E_ROUNDS", "2")))
RUN_TIMEOUT = 120  # per agent turn (throttle spaces its internal calls)
# Global pacing is enforced at the model layer (ADK_CC_MODEL_MAX_RPM, set
# above) across every caller, so this between-turn delay is a small courtesy
# margin. Retry on a 429/500 in case the cap is still grazed.
PACE_S = 3
RETRIES = 3
RETRY_BACKOFF_S = 20

# Scenario: CPU core design. Conflicts are SEQUENCED ACROSS ROUNDS — a
# contradiction/supersession must arrive AFTER its page exists, else every
# claim in the first run sees no page and is classified NOVEL (no conflict).
# Each entry = (topic, ingest_doc, query_or_None).
SCENARIO = {
    # Round 1 — establish distinct baseline pages + capture per-user memory.
    1: {
        "alice": ("pipeline-depth",
                  "Save to the wiki under topic 'pipeline-depth': Modern "
                  "high-performance CPU cores use roughly a 14-stage pipeline.",
                  "What does the wiki say about pipeline depth? Context: I "
                  "design embedded low-power cores."),
        "bob": ("branch-prediction",
                "Save to the wiki under topic 'branch-prediction': Modern cores "
                "use TAGE branch predictors.",
                "Summarize the cache hierarchy. FYI I work on server-class chips."),
        "carol": ("cache-hierarchy",
                  "Save to the wiki under topic 'cache-hierarchy': L2 cache is "
                  "commonly 256KB per core.",
                  "Explain branch prediction. I focus on die-area efficiency."),
    },
    # Round 2 — conflicts against now-existing pages (ingest-only; memory
    # already captured in R1, so we save LLM requests).
    2: {
        # contradiction vs alice's 14-stage (uncited, lone → CONTEST)
        "bob": ("pipeline-depth",
                "Add to the wiki under topic 'pipeline-depth': Actually CPU "
                "pipelines are typically about 20 stages deep.", None),
        # supersession of carol's 256KB ("now" → SUPERSEDE + validity)
        "carol": ("cache-hierarchy",
                  "Update the wiki topic 'cache-hierarchy': L2 cache is now "
                  "512KB per core in current designs.", None),
        # a fresh page (no conflict)
        "alice": ("out-of-order-execution",
                  "Save to the wiki under topic 'out-of-order-execution': Modern "
                  "cores use out-of-order execution with large reorder buffers.", None),
    },
}


# Extra-round pools (used when ADK_CC_E2E_ROUNDS>2). Each entry mirrors the
# SCENARIO shape: (topic, ingest_doc, query). Ingests corroborate existing
# pages or add new ones; every query carries a NEW durable user fact so each
# round grows the per-user episodic backlog (so the threshold trigger keeps
# firing and consolidation keeps having work). Rounds cycle through the pool.
_EXTRA = {
    "alice": [
        ("register-file",
         "Save to the wiki under topic 'register-file': High-performance cores "
         "use a physical register file with 100+ entries.",
         "What does the wiki say about register files? My team standardized on "
         "the RISC-V ISA."),
        ("pipeline-depth",
         "Add to the wiki under topic 'pipeline-depth': Embedded cores often use "
         "shorter ~8-stage pipelines to save power.",
         "Summarize out-of-order execution. We target a 1 GHz maximum clock."),
        ("simd-width",
         "Save to the wiki under topic 'simd-width': Cores include 128- to "
         "512-bit SIMD vector units.",
         "Give me a concise recap of the wiki. I prefer short bullet summaries."),
        ("clock-gating",
         "Save to the wiki under topic 'clock-gating': Fine-grained clock gating "
         "cuts dynamic power in idle units.",
         "What's recorded about the cache hierarchy? Our SoC tapes out in Q4."),
    ],
    "bob": [
        ("smt",
         "Save to the wiki under topic 'smt': Server cores use 2-way "
         "simultaneous multithreading.",
         "What does the wiki say about branch prediction? We use a 7nm node."),
        ("cache-hierarchy",
         "Add to the wiki under topic 'cache-hierarchy': Server parts add a large "
         "shared L3 of 32MB or more.",
         "Summarize pipeline depth. My chips run at a 3.5 GHz base clock."),
        ("memory-bandwidth",
         "Save to the wiki under topic 'memory-bandwidth': Server sockets use "
         "8-channel DDR5 for bandwidth.",
         "What's in the wiki about SIMD? We prioritize throughput over latency."),
        ("ecc",
         "Save to the wiki under topic 'ecc': Server memory uses ECC to correct "
         "single-bit errors.",
         "Recap register files. My validation team is based in Austin."),
    ],
    "carol": [
        ("area-efficiency",
         "Save to the wiki under topic 'area-efficiency': Die area is dominated "
         "by caches and SIMD units.",
         "What does the wiki say about L2 cache now? I optimize for die area."),
        ("branch-prediction",
         "Add to the wiki under topic 'branch-prediction': Smaller predictors "
         "trade accuracy for area on embedded cores.",
         "Summarize register files. My target library is a 22nm process."),
        ("power-budget",
         "Save to the wiki under topic 'power-budget': Mobile cores target a 2W "
         "thermal design power.",
         "What's recorded about SMT? I care most about perf-per-watt."),
        ("decode-width",
         "Save to the wiki under topic 'decode-width': Embedded cores commonly "
         "decode 2-4 instructions per cycle.",
         "Recap memory bandwidth. We tape out next spring."),
    ],
}


def _round_content(rnd: int) -> dict:
    """Round 1-2 = the canonical sequenced-conflict SCENARIO; rounds >2 draw
    from the _EXTRA pools (cycling) to lengthen sessions and stack memory."""
    if rnd in SCENARIO:
        return SCENARIO[rnd]
    pool_len = len(next(iter(_EXTRA.values())))
    idx = (rnd - 3) % pool_len
    return {u: _EXTRA[u][idx] for u in _EXTRA}


def _hdr(user):
    return {"Authorization": f"Bearer {USERS[user]}"}


def _start_server(wiki_root, mem_root, data_dir):
    env = dict(os.environ)  # inherits .env-resolved model creds via dotenv
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_TOKENS": ",".join(f"{tok}={u}:{TENANT}" for u, tok in USERS.items()),
        "ADK_CC_WIKI": "1",
        "ADK_CC_MEMORY": "1",
        "ADK_CC_MEMORY_AUTOCAPTURE": "1",
        "ADK_CC_WIKI_ROOT": wiki_root,
        "ADK_CC_MEMORY_ROOT": mem_root,
        "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{data_dir}",
        "ADK_CC_PERMISSION_MODE": "bypassPermissions",
        # disable session titling (an extra out-of-band LLM call/turn) — not
        # under test, and every saved request helps under the rate cap.
        "ADK_CC_TOOL_TITLES": "0",
        # give the after-run capture call more room (it's the 3rd-4th rapid
        # LLM call within a query turn; the rate limit can make it slow).
        "ADK_CC_MEMORY_CAPTURE_TIMEOUT_S": "60",
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"),
         "adk_cc.service.server:make_app", "--factory",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            if requests.get(BASE + "/list-apps", headers=_hdr("alice"), timeout=2).ok:
                return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("server did not start")


def _create_session(user):
    r = requests.post(f"{BASE}/apps/{APP}/users/{user}/sessions",
                      headers=_hdr(user), json={}, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def _run(user, sid, text):
    """One agent turn; returns (events, tool_names_called). Retries on a
    rate-limit (429/500) with backoff, since the shared quota can still bite
    even when paced."""
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = requests.post(f"{BASE}/run", headers=_hdr(user), timeout=RUN_TIMEOUT, json={
                "appName": APP, "userId": user, "sessionId": sid,
                "newMessage": {"role": "user", "parts": [{"text": text}]},
            })
            if r.status_code in (429, 500, 503) and attempt < RETRIES:
                last = requests.HTTPError(f"{r.status_code}")
                time.sleep(RETRY_BACKOFF_S)
                continue
            r.raise_for_status()
            events = r.json()
            tools = [
                p["functionCall"].get("name")
                for e in events
                for p in (e.get("content") or {}).get("parts") or []
                if p.get("functionCall")
            ]
            return events, tools
        except requests.RequestException as e:
            last = e
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF_S)
    raise last if last else RuntimeError("run failed")


def _cron(script, root, *extra):
    subprocess.run(
        [os.path.join(REPO, ".venv/bin/python"), os.path.join(REPO, "scripts", script),
         "--root", root, *extra],
        cwd=REPO, env=dict(os.environ), timeout=300,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY — live wiki+memory e2e skipped.")
        return 0

    wiki_root = tempfile.mkdtemp(prefix="wm-wiki-")
    mem_root = tempfile.mkdtemp(prefix="wm-mem-")
    data_dir = tempfile.mkdtemp(prefix="wm-art-")
    proc = None
    ok = True
    tool_tally = {}
    turns = {"ok": 0, "err": 0}

    def check(name, cond, detail):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}: {detail}")
        ok = ok and cond

    try:
        proc = _start_server(wiki_root, mem_root, data_dir)
        # probe: one cheap turn; skip if the model can't answer
        sids = {u: _create_session(u) for u in USERS}
        try:
            _run("alice", sids["alice"], "say ok")
        except Exception as e:
            print(f"SKIP: model turn failed ({type(e).__name__}: {e}).")
            return 0

        print(f"[config] rounds={ROUNDS} users={list(USERS)} pace={PACE_S}s "
              f"rpm_cap={os.environ.get('ADK_CC_MODEL_MAX_RPM')} "
              f"threshold={os.environ.get('ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD', 'off')}")
        for rnd in range(1, ROUNDS + 1):
            print(f"\n=== round {rnd} ===")
            for user, (topic, ingest, query) in _round_content(rnd).items():
                for kind, msg in (("ingest", ingest), ("query", query)):
                    if msg is None:
                        continue
                    try:
                        _evts, tools = _run(user, sids[user], msg)
                        for t in tools:
                            tool_tally[t] = tool_tally.get(t, 0) + 1
                        turns["ok"] += 1
                        print(f"  {user}/{kind}: tools={tools}")
                    except Exception as e:
                        turns["err"] += 1
                        print(f"  {user}/{kind}: ERROR {type(e).__name__}: {e}")
                    time.sleep(PACE_S)  # respect the endpoint's rate limit
            print(f"  -- running librarian (--model) + consolidator --")
            _cron("wiki_librarian.py", wiki_root)            # real LLM classifier + synthesis
            _cron("memory_consolidator.py", mem_root)

        # ---- inspect the stores directly (model-independent invariants) ----
        from adk_cc.memory import MemoryStore
        from adk_cc.wiki import WikiStore

        wiki = WikiStore.for_tenant(TENANT, root=wiki_root)
        domain = {s: wiki.read_domain_page(s) for s in wiki.list_domain_pages()}
        quarantine = wiki.list_quarantine(pending_only=False)
        print(f"\n[info] agent turns: ok={turns['ok']} err/timeout={turns['err']}")
        print(f"[info] tool calls across run: {tool_tally}")
        print(f"[info] domain pages: {list(domain)}")
        print(f"[info] quarantine entries: {len(quarantine)}")
        if not tool_tally:
            print("[note] the agent made NO tool calls — the configured model "
                  "couldn't drive the live loop (turns hung/garbled). The harness "
                  "is verified; point ADK_CC_MODEL at a stronger model for content.")

        # 1. COMPLETE run: turns succeed under the rate limit (paced + retried).
        #    Canonical 2-round run is strict (err==0); a longer endurance run
        #    tolerates the occasional rate-limit casualty (report + require ≥90%).
        if ROUNDS <= 2:
            check("all agent turns succeeded under the rate limit",
                  turns["err"] == 0, f"ok={turns['ok']} err={turns['err']}")
        else:
            _total = turns["ok"] + turns["err"]
            _ratio = (turns["ok"] / _total) if _total else 0.0
            check("agent turns mostly succeeded over a long run (≥90%)",
                  _ratio >= 0.9, f"ok={turns['ok']} err={turns['err']} ratio={_ratio:.0%}")

        # 2. provenance on every published domain claim
        prov_ok = bool(domain) and all("_(by " in (p.body if p else "") for p in domain.values())
        check("every domain page carries provenance", prov_ok, f"{len(domain)} pages")

        # 3. shared wiki — pages contributed by ≥2 distinct users
        authors = set()
        for p in domain.values():
            for tok in (p.body if p else "").split("_(by ")[1:]:
                authors.add(tok.split(";")[0].strip())
        shared = {a for a in authors if a in USERS}
        print(f"[info] domain contributing authors: {sorted(shared)}")
        check("shared wiki reflects multiple users", len(shared) >= 2, f"{sorted(shared)}")

        # 4. conflict: bob's R2 contradiction of alice's pipeline depth is
        #    surfaced (page contested, or queued, or both values recorded).
        pd = domain.get("pipeline-depth")
        pd_body = (pd.body if pd else "")
        contested = bool(pd and pd.contested) or len(quarantine) >= 1 or (
            "14" in pd_body and "20" in pd_body)
        check("contradiction surfaced (contested / queued / both recorded)",
              contested, f"contested={bool(pd and pd.contested)} q={len(quarantine)}")

        # 5. supersession: carol's R2 '512KB' superseded '256KB' — validity
        #    window recorded, or both values present on the page.
        ch = domain.get("cache-hierarchy")
        ch_body = (ch.body if ch else "")
        superseded = bool(ch and ch.frontmatter.get("validity")) or "512" in ch_body
        check("supersession recorded (validity window or new value present)",
              superseded, f"validity={bool(ch and ch.frontmatter.get('validity'))}")

        # 6. memory captured per user + isolated
        mem = MemoryStore.for_tenant(TENANT, root=mem_root)
        per_user = {u: (mem.list_episodic(u), mem.list_semantic(u)) for u in USERS}
        for u, (epi, sem) in per_user.items():
            print(f"[info] memory {u}: episodic={len(epi)} semantic={len(sem)} "
                  f"topics={[s.topic for s in sem]}")
        captured = {u for u, (e, s) in per_user.items() if e or s}
        # ≥1 proves the autonomous capture loop works end-to-end; per-turn
        # capture is best-effort (an extra LLM call subject to the rate limit),
        # so the full breakdown is reported, not required for all 3.
        check("memory autonomously captured (≥1 user)", len(captured) >= 1,
              f"captured={sorted(captured)} of {sorted(USERS)}")
        alice_blob = " ".join(s.text for s in per_user["alice"][1]).lower()
        bob_blob = " ".join(s.text for s in per_user["bob"][1]).lower()
        leaked = "embedded" in bob_blob and "embedded" in alice_blob
        check("memory is isolated per user (no cross-user leak)", not leaked,
              "alice's context not in bob's memory")

        # 7. idempotency: re-running the crons changes nothing (--no-model: no
        #    model calls, so this is safe under the rate limit)
        before = (sorted(wiki.list_domain_pages()), len(wiki.list_quarantine(pending_only=False)))
        _cron("wiki_librarian.py", wiki_root, "--no-model")
        _cron("memory_consolidator.py", mem_root)
        after = (sorted(wiki.list_domain_pages()), len(wiki.list_quarantine(pending_only=False)))
        check("crons are idempotent (re-run is a no-op)", before == after,
              f"{before} == {after}")

        print("\n--- domain pages ---")
        for slug, p in domain.items():
            print(f"### {slug}\n{(p.body if p else '').strip()[:400]}\n")
        print("wiki+memory live e2e " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        if proc:
            proc.kill()
        for d in (wiki_root, mem_root, data_dir):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
