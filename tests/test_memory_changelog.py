"""Fix G tests: memory changelog + topic index + revert. Model-free."""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.memory import MemoryStore, consolidate_user
from adk_cc.memory.store import ARCHIVED, EPISODIC, MemoryItem, SEMANTIC


def _store() -> MemoryStore:
    return MemoryStore.for_tenant("acme", root=tempfile.mkdtemp(prefix="cl-mem-"))


def test_capture_is_logged():
    s = _store()
    s.add_episodic("alice", "deploys to fly", topic="deploy")
    log = s.read_changelog("alice")
    assert len(log) == 1 and log[0]["op"] == "capture", log
    assert log[0]["topic"] == "deploy" and log[0]["tier"] == EPISODIC


def test_semantic_ops_logged_and_indexed():
    s = _store()
    s.put_semantic("alice", MemoryItem(id="db", topic="db", text="uses sqlite"))
    s.put_semantic("alice", MemoryItem(id="db", topic="db", text="uses postgres",
                                       supersedes=["uses sqlite"]))
    s.put_semantic("alice", MemoryItem(id="db", topic="db", text="uses postgres"))  # no change
    ops = [e["op"] for e in s.read_changelog("alice")]
    assert ops == ["semantic_create", "semantic_supersede", "semantic_corroborate"], ops
    idx = s.get_topic_index("alice")
    assert "db" in idx and idx["db"]["summary"].startswith("uses postgres"), idx


def test_consolidate_populates_changelog_and_index():
    s = _store()
    s.add_episodic("alice", "team chose Postgres", topic="datastore")
    s.add_episodic("alice", "team chose Postgres 16", topic="datastore")
    consolidate_user(s, "alice")
    idx = s.get_topic_index("alice")
    assert "datastore" in idx, idx
    ops = [e["op"] for e in s.read_changelog("alice")]
    assert "capture" in ops and any(o.startswith("semantic_") for o in ops), ops


def test_revert_restores_prior_value():
    s = _store()
    s.put_semantic("alice", MemoryItem(id="db", topic="db", text="uses sqlite"))
    s.put_semantic("alice", MemoryItem(id="db", topic="db", text="uses postgres",
                                       supersedes=["uses sqlite"]))
    assert s.get_semantic("alice", "db").text == "uses postgres"
    assert s.revert_semantic("alice", "db") is True
    assert s.get_semantic("alice", "db").text == "uses sqlite"
    assert s.revert_semantic("alice", "db") is False  # nothing left to revert
    assert s.read_changelog("alice")[-1]["op"] == "revert"


def test_archive_drops_from_index():
    s = _store()
    s.put_semantic("alice", MemoryItem(id="old", topic="old", text="stale fact"))
    assert "old" in s.get_topic_index("alice")
    s.set_status("alice", SEMANTIC, "old", ARCHIVED)
    assert "old" not in s.get_topic_index("alice")
    assert s.read_changelog("alice")[-1]["op"] == f"status:{ARCHIVED}"


def test_changelog_is_per_user_isolated():
    s = _store()
    s.add_episodic("alice", "alice fact", topic="a")
    s.add_episodic("bob", "bob fact", topic="b")
    assert len(s.read_changelog("alice")) == 1
    assert len(s.read_changelog("bob")) == 1
    assert s.read_changelog("alice")[0]["user"] == "alice"


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
    print("\nall changelog tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
