"""E2E (model-connected, slow pace): drive the REAL agent to capture memory and
add wiki docs, then assert the knowledge-graph nodes are created correctly.

Runs against a live model, paced SLOWLY with backoff on 429/500/timeout (the
endpoint is shared + rate-limited). Memory flows through capture→consolidate;
wiki flows through wiki_add→librarian (the publish step is deterministic / no
model). Assertions are STRUCTURAL (kinds, unique ids, episodic→semantic links,
domain nodes exist) and content-tolerant, since the agent decides specifics.
Skips gracefully if the endpoint is too throttled to land any turn.

Deterministic node-assembly is covered separately in test_graph_routes.py.
Run: .venv/bin/python tests/e2e_knowledge_nodes.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

import adk_cc  # noqa: F401 — bootstraps .env (live model creds)

# Conservative pacing for the shared, rate-limited endpoint.
os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "15")
for _k in ("ADK_CC_WIKI_STORE_URI", "ADK_CC_MEMORY_STORE_URI"):
    os.environ.pop(_k, None)

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"
USER = "local"
PACE_S = 22          # long gap between turns
BACKOFF_S = 30       # wait after a 429/500/timeout before retry
RETRIES = 2

MEMORY = [
    "Remember about me: my name is Jisu and I'm a staff engineer.",
    "Remember: my project adk-cc deploys to Fly.io and uses Postgres 16.",
    "Remember: I prefer concise, bullet-point answers and dark mode.",
]
WIKI = [
    "Save to the wiki under topic 'pipeline-depth': high-performance CPU cores "
    "use roughly a 14-stage pipeline.",
    "Add to the wiki under topic 'branch-prediction': modern cores use TAGE "
    "branch predictors.",
]

ok = True


def check(name, cond, detail=""):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    ok = ok and cond


def _turn(sid: str, text: str, tag: str) -> bool:
    for attempt in range(RETRIES + 1):
        try:
            r = requests.post(f"{BASE}/run", timeout=180, json={
                "appName": "adk_cc", "userId": USER, "sessionId": sid,
                "newMessage": {"role": "user", "parts": [{"text": text}]}})
            if r.status_code in (429, 500, 503):
                print(f"  {tag}: HTTP {r.status_code} (retry {attempt + 1}, backoff {BACKOFF_S}s)")
                time.sleep(BACKOFF_S)
                continue
            tools = [p["functionCall"]["name"] for e in (r.json() if r.ok else [])
                     for p in (e.get("content") or {}).get("parts") or [] if p.get("functionCall")]
            print(f"  {tag}: HTTP {r.status_code} tools={tools}")
            return r.ok
        except requests.RequestException as e:
            print(f"  {tag}: {type(e).__name__} (retry {attempt + 1}, backoff {BACKOFF_S}s)")
            time.sleep(BACKOFF_S)
    return False


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY.")
        return 0

    mem_root = tempfile.mkdtemp(prefix="knodes-mem-")
    wiki_root = tempfile.mkdtemp(prefix="knodes-wiki-")   # MUST differ from mem_root
    wks = tempfile.mkdtemp(prefix="knodes-wks-")
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_KNOWLEDGE_UI": "1",
        "ADK_CC_WIKI": "1", "ADK_CC_MEMORY": "1",
        "ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD": "2",
        "ADK_CC_MEMORY_CAPTURE_TIMEOUT_S": "90",
        "ADK_CC_WIKI_ROOT": wiki_root, "ADK_CC_MEMORY_ROOT": mem_root,
        "ADK_CC_WORKSPACE_ROOT": wks, "ADK_CC_TOOL_TITLES": "0",
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

        sid = requests.post(f"{BASE}/apps/adk_cc/users/{USER}/sessions",
                            json={}, timeout=15).json()["id"]
        # warm-up: bail to SKIP if even one cheap turn can't land
        if not _turn(sid, "say ok", "warmup"):
            print("SKIP: endpoint unreachable / rate-limited (warmup failed).")
            return 0

        landed = 0
        print("== memory turns (slow) ==")
        for i, m in enumerate(MEMORY, 1):
            landed += 1 if _turn(sid, m, f"mem{i}") else 0
            time.sleep(PACE_S)
        print("== wiki turns (slow) ==")
        for i, w in enumerate(WIKI, 1):
            landed += 1 if _turn(sid, w, f"wiki{i}") else 0
            time.sleep(PACE_S)

        if landed == 0:
            print("SKIP: no turn landed (endpoint too throttled).")
            return 0

        # publish wiki inbox → domain (deterministic, no model call)
        print("== publish wiki (librarian --no-model) ==")
        subprocess.run(
            [os.path.join(REPO, ".venv/bin/python"), os.path.join(REPO, "scripts/wiki_librarian.py"),
             "--root", wiki_root, "--no-model"],
            cwd=REPO, env=env, timeout=120,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # ---------- assert node correctness ----------
        mg = requests.get(BASE + "/api/knowledge/memory/graph", timeout=10).json()
        wg = requests.get(BASE + "/api/knowledge/wiki/graph", timeout=10).json()
        print(f"\n  memory: {len(mg['nodes'])} nodes / {len(mg['links'])} links")
        print(f"  wiki:   {len(wg['nodes'])} nodes / {len(wg['links'])} links")
        print(f"  memory topics: {sorted({n['topic'] for n in mg['nodes']})}")
        print(f"  wiki domain: {[n['label'] for n in wg['nodes'] if n['kind']=='domain']}")

        sem = [n for n in mg["nodes"] if n["kind"] == "semantic"]
        epi = [n for n in mg["nodes"] if n["kind"] == "episodic"]
        mids = [n["id"] for n in mg["nodes"]]
        check("memory: captured ≥1 episodic node", len(epi) >= 1, f"{len(epi)}")
        check("memory: consolidated ≥1 semantic node", len(sem) >= 1, f"{len(sem)}")
        check("memory: kinds are semantic|episodic only",
              all(n["kind"] in ("semantic", "episodic") for n in mg["nodes"]))
        check("memory: node ids unique", len(mids) == len(set(mids)))
        check("memory: links are episodic→semantic with valid endpoints",
              all(l["source"].startswith("epi:") and l["target"].startswith("sem:")
                  and l["source"] in set(mids) and l["target"] in set(mids)
                  for l in mg["links"]))

        wids = [n["id"] for n in wg["nodes"]]
        dom = [n for n in wg["nodes"] if n["kind"] == "domain"]
        check("wiki: ≥1 domain node published", len(dom) >= 1, f"{len(dom)}")
        check("wiki: kinds are domain|inbox only",
              all(n["kind"] in ("domain", "inbox") for n in wg["nodes"]))
        check("wiki: node ids unique (no dup blue/green)", len(wids) == len(set(wids)), f"{wids}")
        check("wiki: links reference existing nodes",
              all(l["source"] in set(wids) for l in wg["links"]))

        print("\nknowledge nodes e2e " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        proc.kill()
        for d in (mem_root, wiki_root, wks):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
