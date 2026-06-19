"""Tests for the autonomous memory store/recall/consolidate (Phase B).

Covers MemoryStore (episodic/semantic tiers on the docstore, lifecycle,
access tracking, search), recall_context (budgeted, semantic-first), and
consolidate_user (episodic→semantic latest-wins + corroboration confidence +
supersession history + staleness archival). Deterministic — no model.
Hand-rolled.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.memory import (
    ACTIVE,
    ARCHIVED,
    CONSOLIDATED,
    SEMANTIC,
    MemoryItem,
    MemoryStore,
    consolidate_user,
    recall_context,
)


def _store(root: str, tenant: str = "acme") -> MemoryStore:
    return MemoryStore.for_tenant(tenant, root=root)


# ----------------------- store -----------------------
def test_episodic_add_and_list():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        item = st.add_episodic("alice", "User prefers dark mode.", topic="ui-preference")
        assert item.topic == "ui-preference" and item.memory_type == "episodic"
        listed = st.list_episodic("alice")
        assert len(listed) == 1 and listed[0].text == "User prefers dark mode."
        assert st.list_user_ids() == ["alice"]
        # scoped per user
        assert st.list_episodic("bob") == []
    print("OK episodic_add_and_list")


def test_semantic_roundtrip_and_status():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.put_semantic("alice", MemoryItem(id="name", topic="name",
                        text="User's name is Jisu.", confidence=0.8))
        got = st.get_semantic("alice", "name")
        assert got is not None and got.confidence == 0.8 and got.memory_type == SEMANTIC
        assert st.set_status("alice", SEMANTIC, "name", ARCHIVED) is True
        assert st.get_semantic("alice", "name").status == ARCHIVED
        assert st.list_semantic("alice", status=ARCHIVED)
        assert st.list_semantic("alice", status=ACTIVE) == []
    print("OK semantic_roundtrip_and_status")


def test_record_access():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.put_semantic("alice", MemoryItem(id="t", topic="t", text="fact"))
        st.record_access("alice", "t")
        st.record_access("alice", "t")
        assert st.get_semantic("alice", "t").access_count == 2
    print("OK record_access")


def test_search_semantic_and_episodic():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.put_semantic("alice", MemoryItem(id="stack", topic="stack",
                        text="The project uses FastAPI and React."))
        st.add_episodic("alice", "User mentioned deploying to Fly.io.", topic="deploy")
        hits = st.search("alice", "FastAPI deploy", limit=5)
        cols = {h.collection for h in hits}
        assert any(c.endswith("/semantic") for c in cols)
        assert st.search("alice", "zzzqqq", limit=5) == []
    print("OK search_semantic_and_episodic")


# ----------------------- recall -----------------------
def test_recall_budgeted_semantic_first():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.put_semantic("alice", MemoryItem(id="name", topic="name",
                        text="User's name is Jisu.", confidence=0.9))
        st.add_episodic("alice", "Jisu asked about caching yesterday.", topic="caching")
        block = recall_context(st, "alice", "what is the user name", budget_tokens=400)
        assert "Memory (recalled" in block and "Known facts" in block
        assert "Jisu" in block
        # empty query / no match → empty
        assert recall_context(st, "alice", "") == ""
        assert recall_context(st, "alice", "zzzqqq nonsense") == ""
        # budget caps length
        tiny = recall_context(st, "alice", "Jisu name caching", budget_tokens=20)
        assert len(tiny) <= 20 * 4
    print("OK recall_budgeted_semantic_first")


# ----------------------- consolidation -----------------------
def test_consolidate_creates_semantic_and_marks_episodic():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.add_episodic("alice", "User's name is Jisu.", topic="name")
        rep = consolidate_user(st, "alice")
        assert rep.created == 1 and rep.topics_consolidated == 1
        sem = st.get_semantic("alice", "name")
        assert sem is not None and "Jisu" in sem.text and sem.status == ACTIVE
        # source episodic now marked consolidated (not reprocessed next run)
        assert st.list_episodic("alice", status=CONSOLIDATED)
        rep2 = consolidate_user(st, "alice")
        assert rep2.topics_consolidated == 0, "consolidated episodics aren't reprocessed"
    print("OK consolidate_creates_semantic_and_marks_episodic")


def test_consolidate_latest_wins_with_supersession():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        # two captures on one topic, explicit ordered timestamps via created
        st.add_episodic("alice", "User prefers light mode.", topic="theme",
                        doc_id="theme__a")
        # force an older timestamp on the first, newer on the second
        old = st.get_semantic  # noqa: no-op ref to keep linter calm
        e1 = st.list_episodic("alice")[0]
        # second capture (newer)
        st.add_episodic("alice", "Actually the user now prefers dark mode.",
                        topic="theme", doc_id="theme__b")
        # ensure deterministic ordering: rewrite created stamps
        _force_created(st, "alice", "theme__a", "2026-01-01T00:00:00Z")
        _force_created(st, "alice", "theme__b", "2026-02-01T00:00:00Z")
        consolidate_user(st, "alice")
        sem = st.get_semantic("alice", "theme")
        assert "dark mode" in sem.text, sem.text          # latest wins
        assert any("light mode" in s for s in sem.supersedes)  # old kept as history
        assert sem.confidence > 0.5                        # corroboration bump
    print("OK consolidate_latest_wins_with_supersession")


def test_consolidate_staleness_archives():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.put_semantic("alice", MemoryItem(id="old", topic="old",
                        text="stale fact", status=ACTIVE,
                        created="2025-01-01T00:00:00Z", updated="2025-01-01T00:00:00Z"))
        # now = far future; stale_days small → archived
        import time as _t
        now = _t.mktime(_t.strptime("2026-06-01T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        rep = consolidate_user(st, "alice", stale_days=30, now_epoch=now)
        assert rep.archived_stale == 1
        assert st.get_semantic("alice", "old").status == ARCHIVED
    print("OK consolidate_staleness_archives")


def _force_created(st: MemoryStore, user_id: str, doc_id: str, ts: str) -> None:
    col = MemoryStore.EPISODIC_OF(user_id)
    doc = st.store.get_doc(col, doc_id)
    doc.frontmatter["created"] = ts
    st.store.put_doc(col, doc)


def main():
    test_episodic_add_and_list()
    test_semantic_roundtrip_and_status()
    test_record_access()
    test_search_semantic_and_episodic()
    test_recall_budgeted_semantic_first()
    test_consolidate_creates_semantic_and_marks_episodic()
    test_consolidate_latest_wins_with_supersession()
    test_consolidate_staleness_archives()
    print("\nall memory-store tests passed")


if __name__ == "__main__":
    main()
