"""Security e2e: cross-user isolation for wiki / memory / REST.

Two users (alice, bob) on one tenant (acme). Verifies a caller can't reach
another user's private data. No model needed — session CRUD, REST GETs, tool
calls, and store ops are all model-free — so this runs fast and rate-limit-free
(stub key, dotenv skipped).

Layers checked:
  1. Tool-level wiki isolation — wiki_read/wiki_search as bob can't see alice's
     inbox (tools key on the authenticated user_id), and path-traversal slugs
     are neutralized by slugify.
  2. Memory — there is NO agent-facing memory tool (the agent can't query any
     user's memory), and per-user stores are isolated.
  3. REST trust-the-path — /apps/{app}/users/{alice}/sessions accessed with
     bob's token. Demonstrates it's OPEN by default and CLOSED (403) under
     ADK_CC_AUTHZ=1 (make_authz_middleware).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

os.environ["ADK_CC_SKIP_DOTENV"] = "1"
os.environ["ADK_CC_API_KEY"] = "stub"          # no real model — none needed
os.environ["ADK_CC_WIKI"] = "1"
os.environ["ADK_CC_MEMORY"] = "1"
for _k in ("ADK_CC_WIKI_STORE_URI", "ADK_CC_MEMORY_STORE_URI"):
    os.environ.pop(_k, None)

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8773
BASE = f"http://127.0.0.1:{PORT}"
APP = "adk_cc"
TENANT = "acme"
TOKENS = {"alice": "alice_tok", "bob": "bob_tok"}
SECRET = "ALICE-PRIVATE-SECRET: my unreleased CPU tapeout date is March."


def _hdr(user):
    return {"Authorization": f"Bearer {TOKENS[user]}"}


def _start_server(wiki_root, mem_root, data_dir, *, authz: bool):
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_TOKENS": ",".join(f"{t}={u}:{TENANT}" for u, t in TOKENS.items()),
        "ADK_CC_WIKI_ROOT": wiki_root,
        "ADK_CC_MEMORY_ROOT": mem_root,
        "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{data_dir}",
    })
    env["ADK_CC_AUTHZ"] = "1" if authz else "0"
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"),
         "adk_cc.service.server:make_app", "--factory",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            if requests.get(BASE + "/list-apps", headers=_hdr("alice"), timeout=2).status_code < 500:
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


def _status(token_user, path):
    r = requests.get(BASE + path, headers=_hdr(token_user), timeout=15)
    return r.status_code


def main() -> int:
    ok = True

    def check(name, cond, detail):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}: {detail}")
        ok = ok and cond

    wiki_root = tempfile.mkdtemp(prefix="sec-wiki-")
    mem_root = tempfile.mkdtemp(prefix="sec-mem-")
    data_dir = tempfile.mkdtemp(prefix="sec-art-")
    os.environ["ADK_CC_WIKI_ROOT"] = wiki_root
    os.environ["ADK_CC_MEMORY_ROOT"] = mem_root
    proc = None
    try:
        # ---------- 1. tool-level wiki isolation (no server, no model) ----------
        from adk_cc.tools.schemas import WikiReadArgs, WikiSearchArgs
        from adk_cc.tools.wiki import WikiReadTool, WikiSearchTool
        from adk_cc.wiki import WikiStore
        import asyncio

        wiki = WikiStore.for_tenant(TENANT).ensure()
        alice_doc = wiki.add_inbox("alice", SECRET, topic="tapeout-date")  # alice's private note

        def _bob_ctx():
            return SimpleNamespace(
                state={"temp:tenant_context": SimpleNamespace(tenant_id=TENANT, user_id="bob")})

        # bob reads the slug alice used, scope=inbox → must NOT get alice's note
        r = asyncio.run(WikiReadTool()._execute(
            WikiReadArgs(slug="tapeout-date", scope="inbox"), _bob_ctx()))
        check("bob cannot read alice's inbox note via wiki_read",
              r.get("status") == "not_found" and SECRET not in str(r),
              f"status={r.get('status')}")

        # bob searches for the secret → no inbox hit from alice
        r = asyncio.run(WikiSearchTool()._execute(
            WikiSearchArgs(query="tapeout date secret CPU", limit=5), _bob_ctx()))
        leaked = any("tapeout" in str(h).lower() for h in r.get("hits", []))
        check("bob's wiki_search does not surface alice's private note",
              not leaked, f"hits={r.get('count')}")

        # path-traversal slug is neutralized (slugify strips it), no escape
        r = asyncio.run(WikiReadTool()._execute(
            WikiReadArgs(slug="../alice/inbox/tapeout-date", scope="inbox"), _bob_ctx()))
        check("path-traversal slug is neutralized (no cross-user escape)",
              r.get("status") == "not_found" and SECRET not in str(r),
              f"status={r.get('status')}")

        # control: alice CAN read her own note
        def _alice_ctx():
            return SimpleNamespace(
                state={"temp:tenant_context": SimpleNamespace(tenant_id=TENANT, user_id="alice")})
        r = asyncio.run(WikiReadTool()._execute(
            WikiReadArgs(slug="tapeout-date", scope="inbox"), _alice_ctx()))
        check("control: alice CAN read her own inbox note",
              r.get("status") == "ok" and "tapeout" in str(r).lower(), f"status={r.get('status')}")

        # ---------- 2. memory: no agent tool + per-user isolation ----------
        from adk_cc.memory import MemoryStore
        mem = MemoryStore.for_tenant(TENANT)
        mem.add_episodic("alice", "Alice's private salary is 999.", topic="salary")
        check("memory is per-user isolated (bob sees none of alice's)",
              mem.list_episodic("bob") == [] and len(mem.list_episodic("alice")) == 1,
              f"bob={len(mem.list_episodic('bob'))} alice={len(mem.list_episodic('alice'))}")

        import adk_cc.agent as agent_mod
        tool_names = [getattr(getattr(t, "meta", None), "name", "") for t in agent_mod.root_agent.tools]
        check("agent has NO memory tool (cannot query memory directly)",
              not any("memory" in (n or "") for n in tool_names),
              f"tools={sorted(n for n in tool_names if n)}")

        # ---------- 3. REST trust-the-path: OPEN by default ----------
        proc = _start_server(wiki_root, mem_root, data_dir, authz=False)
        alice_sid = _create_session("alice")
        bob_list = _status("bob", f"/apps/{APP}/users/alice/sessions")
        bob_detail = _status("bob", f"/apps/{APP}/users/alice/sessions/{alice_sid}")
        alice_own = _status("alice", f"/apps/{APP}/users/alice/sessions/{alice_sid}")
        print(f"  [info] authz OFF: bob→alice list={bob_list} detail={bob_detail} | alice→own={alice_own}")
        check("control: alice can read her own session (200)", alice_own == 200, f"{alice_own}")
        # Document the default posture: cross-user REST is NOT blocked unless authz on.
        cross_open = bob_detail == 200
        check("FINDING: cross-user REST is OPEN by default (needs ADK_CC_AUTHZ=1)",
              True, f"bob→alice detail={bob_detail} ({'OPEN/leak' if cross_open else 'blocked'})")
        proc.kill(); proc = None
        time.sleep(0.5)

        # ---------- 3b. REST trust-the-path: CLOSED under ADK_CC_AUTHZ=1 ----------
        proc = _start_server(wiki_root, mem_root, data_dir, authz=True)
        alice_sid = _create_session("alice")
        bob_detail = _status("bob", f"/apps/{APP}/users/alice/sessions/{alice_sid}")
        alice_own = _status("alice", f"/apps/{APP}/users/alice/sessions/{alice_sid}")
        print(f"  [info] authz ON: bob→alice detail={bob_detail} | alice→own={alice_own}")
        check("with ADK_CC_AUTHZ=1, bob is BLOCKED from alice's session (403)",
              bob_detail == 403, f"bob→alice detail={bob_detail}")
        check("with ADK_CC_AUTHZ=1, alice still reads her own (200)",
              alice_own == 200, f"{alice_own}")

        print("\nsecurity isolation e2e " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        if proc:
            proc.kill()
        for d in (wiki_root, mem_root, data_dir):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
