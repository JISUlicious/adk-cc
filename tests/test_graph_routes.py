"""Knowledge-graph endpoints (Task 1). Model-free, via FastAPI TestClient.
Covers graph assembly + the cross-user MEMORY isolation guarantee."""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
os.environ["ADK_CC_KNOWLEDGE_UI"] = "1"
os.environ["ADK_CC_WIKI"] = "1"
os.environ["ADK_CC_MEMORY"] = "1"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from adk_cc.memory import MemoryStore, consolidate_user
from adk_cc.service.graph_routes import mount_knowledge_routes
from adk_cc.wiki import WikiStore
from adk_cc.wiki.page import Page

_ROOT = tempfile.mkdtemp(prefix="graph-")
os.environ["ADK_CC_WIKI_ROOT"] = _ROOT
os.environ["ADK_CC_MEMORY_ROOT"] = _ROOT
for _k in ("ADK_CC_WIKI_STORE_URI", "ADK_CC_MEMORY_STORE_URI"):
    os.environ.pop(_k, None)

# Fixed principal injected per-request by a tiny middleware (simulates auth).
_PRINCIPAL = {"v": ("local", "local")}  # (user_id, tenant_id)


def _client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        class _Auth(tuple):
            pass
        request.state.adk_cc_auth = _Auth(_PRINCIPAL["v"])
        return await call_next(request)

    mount_knowledge_routes(app)
    return TestClient(app)


def _seed():
    w = WikiStore.for_tenant("acme", root=_ROOT).ensure()
    w.write_domain_page(Page("gpu", {"title": "GPU", "sources": ["s1"]},
                             "GPUs use SIMT. See [[cpu]] and [[missing-page]].\n"))
    w.write_domain_page(Page("cpu", {"title": "CPU", "sources": ["s2"]},
                             "CPUs use pipelines.\n"))
    # alice's private memory
    ma = MemoryStore.for_tenant("acme", root=_ROOT)
    for _ in range(2):
        ma.add_episodic("alice", "Alice deploys to Fly.io.", topic="deploy")
    consolidate_user(ma, "alice")


def test_wiki_graph_nodes_and_links():
    _seed()
    _PRINCIPAL["v"] = ("someuser", "acme")
    c = _client()
    g = c.get("/api/knowledge/wiki/graph").json()
    ids = {n["id"] for n in g["nodes"]}
    assert {"gpu", "cpu"} <= ids, ids
    # the [[cpu]] link exists and resolves; [[missing-page]] is flagged missing
    by = {(l["source"], l["target"]): l for l in g["links"]}
    assert ("gpu", "cpu") in by and by[("gpu", "cpu")]["missing"] is False
    assert ("gpu", "missing-page") in by and by[("gpu", "missing-page")]["missing"] is True


def test_wiki_page_content():
    _seed()
    _PRINCIPAL["v"] = ("someuser", "acme")
    c = _client()
    p = c.get("/api/knowledge/wiki/page/gpu").json()
    assert p["status"] == "ok" and p["title"] == "GPU" and "SIMT" in p["body"]
    miss = c.get("/api/knowledge/wiki/page/nope").json()
    assert miss["status"] == "not_found"


def test_memory_graph_scoped_to_caller():
    _seed()
    _PRINCIPAL["v"] = ("alice", "acme")
    c = _client()
    g = c.get("/api/knowledge/memory/graph").json()
    topics = {n["topic"] for n in g["nodes"]}
    assert "deploy" in topics, topics
    assert any(n["kind"] == "semantic" for n in g["nodes"])


def test_memory_isolation_other_user_sees_nothing():
    _seed()  # alice has memory; bob has none
    _PRINCIPAL["v"] = ("bob", "acme")
    c = _client()
    g = c.get("/api/knowledge/memory/graph").json()
    assert g["nodes"] == [], f"bob must not see alice's memory: {g}"


def test_memory_item_detail():
    _seed()
    _PRINCIPAL["v"] = ("alice", "acme")
    c = _client()
    g = c.get("/api/knowledge/memory/graph").json()
    sem = next(n for n in g["nodes"] if n["kind"] == "semantic")
    raw_id = sem["id"].split(":", 1)[1]  # strip the "sem:" prefix (UI does this)
    item = c.get(f"/api/knowledge/memory/item/{raw_id}").json()
    assert item["status"] == "ok", item
    assert item["topic"] == "deploy" and "Fly.io" in item["text"]
    assert "confidence" in item and "supersedes" in item
    # unknown id → not_found
    assert c.get("/api/knowledge/memory/item/nope").json()["status"] == "not_found"


def test_memory_item_isolation():
    _seed()
    # discover alice's semantic id as alice, then try to read it as bob
    _PRINCIPAL["v"] = ("alice", "acme")
    g = _client().get("/api/knowledge/memory/graph").json()
    raw_id = next(n for n in g["nodes"] if n["kind"] == "semantic")["id"].split(":", 1)[1]
    _PRINCIPAL["v"] = ("bob", "acme")
    item = _client().get(f"/api/knowledge/memory/item/{raw_id}").json()
    assert item["status"] == "not_found", f"bob must not read alice's item: {item}"


def test_disabled_when_flag_off():
    os.environ["ADK_CC_KNOWLEDGE_UI"] = "0"
    try:
        app = FastAPI()
        mount_knowledge_routes(app)
        c = TestClient(app)
        # route not mounted → 404
        assert c.get("/api/knowledge/wiki/graph").status_code == 404
    finally:
        os.environ["ADK_CC_KNOWLEDGE_UI"] = "1"


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__[5:]}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__[5:]}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__[5:]}: {type(e).__name__}: {e}")
    print("\nall graph-route tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
