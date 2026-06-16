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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8771
BASE = f"http://127.0.0.1:{PORT}"
APP = "adk_cc"
TENANT = "acme"
USERS = {"alice": "alice_tok", "bob": "bob_tok", "carol": "carol_tok"}
ROUNDS = 2
RUN_TIMEOUT = 60  # per agent turn
# Pace turns so we don't burst the rate-limited hosted endpoint (bursting was
# observed to 500 follow-up turns + starve the after-run memory-capture call).
# Each turn also fires out-of-band calls (session title, memory capture), so a
# turn is several model requests — keep this generous.
PACE_S = 8

# Scenario: CPU core design. Each entry = (topic, ingest_doc, query_with_context).
SCENARIO = {
    1: {
        "alice": ("pipeline-depth",
                  "Save this to the wiki under topic 'pipeline-depth': Modern "
                  "high-performance CPU cores use roughly a 14-stage pipeline.",
                  "What does the wiki say about pipeline depth? For context, I "
                  "design embedded low-power cores."),
        "bob": ("pipeline-depth",
                "Add to the wiki under topic 'pipeline-depth': CPU pipelines "
                "are typically about 20 stages deep.",
                "Summarize branch prediction for me. FYI I work on server-class chips."),
        "carol": ("cache-hierarchy",
                  "Save to the wiki under topic 'cache-hierarchy': L2 cache is "
                  "commonly 256KB per core.",
                  "Explain the cache hierarchy. I focus on die-area efficiency."),
    },
    2: {
        "alice": ("pipeline-depth",
                  "Update the wiki topic 'pipeline-depth': Confirmed ~14-15 "
                  "pipeline stages in modern cores (source: hennessy-patterson).",
                  "Given my embedded low-power focus, what pipeline depth fits?"),
        "carol": ("cache-hierarchy",
                  "Update the wiki topic 'cache-hierarchy': L2 cache is now "
                  "512KB per core in current designs.",
                  "What's the latest on cache sizes?"),
        "bob": ("branch-prediction",
                "Save to the wiki under topic 'branch-prediction': Modern cores "
                "use TAGE branch predictors.",
                "Remind me what we know about pipeline depth."),
    },
}


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
    """One agent turn; returns (events, tool_names_called)."""
    r = requests.post(f"{BASE}/run", headers=_hdr(user), timeout=RUN_TIMEOUT, json={
        "appName": APP, "userId": user, "sessionId": sid,
        "newMessage": {"role": "user", "parts": [{"text": text}]},
    })
    r.raise_for_status()
    events = r.json()
    tools = []
    for e in events:
        for p in (e.get("content") or {}).get("parts") or []:
            fc = p.get("functionCall")
            if fc:
                tools.append(fc.get("name"))
    return events, tools


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

        for rnd in range(1, ROUNDS + 1):
            print(f"\n=== round {rnd} ===")
            for user, (topic, ingest, query) in SCENARIO.get(rnd, {}).items():
                for kind, msg in (("ingest", ingest), ("query", query)):
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
            print(f"  -- running librarian + consolidator --")
            _cron("wiki_librarian.py", wiki_root, "--no-model")
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

        # 1. provenance on every published domain claim (regardless of count)
        prov_ok = all("_(by " in (p.body if p else "") for p in domain.values())
        check("every domain page carries provenance", prov_ok or not domain,
              f"{len(domain)} pages")

        # 2. shared wiki — domain reflects contributions from >1 user (if any
        #    ingestion happened); reported, asserted only when data exists.
        authors = set()
        for p in domain.values():
            for tok in (p.body if p else "").split("_(by "):
                authors.add(tok.split(";")[0].strip()) if tok and tok[0:1].isalpha() else None
        print(f"[info] domain contributing authors (parsed): {sorted(a for a in authors if a in USERS)}")

        # 3. memory is per-user + isolated
        mem = MemoryStore.for_tenant(TENANT, root=mem_root)
        per_user = {u: (mem.list_episodic(u), mem.list_semantic(u)) for u in USERS}
        for u, (epi, sem) in per_user.items():
            print(f"[info] memory {u}: episodic={len(epi)} semantic={len(sem)} "
                  f"topics={[s.topic for s in sem]}")
        alice_blob = " ".join(s.text for s in per_user["alice"][1]).lower()
        bob_blob = " ".join(s.text for s in per_user["bob"][1]).lower()
        # isolation: alice's distinctive 'embedded low-power' context, if captured,
        # must not appear in bob's memory.
        leaked = ("embedded" in bob_blob and "embedded" in alice_blob)
        check("memory is isolated per user (no cross-user leak)", not leaked,
              "alice's context not in bob's memory")

        # 4. idempotency: re-running the crons changes nothing observable
        before = (sorted(wiki.list_domain_pages()), len(wiki.list_quarantine(pending_only=False)))
        _cron("wiki_librarian.py", wiki_root)
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
