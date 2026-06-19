"""Fix C tests: prune-on-consolidate caps the episodic tier. Model-free."""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.memory import MemoryStore, consolidate_user
from adk_cc.memory.store import ARCHIVED, CONSOLIDATED


def _store():
    return MemoryStore.for_tenant("acme", root=tempfile.mkdtemp(prefix="prune-mem-"))


def _seed(s, n):
    for i in range(n):
        s.add_episodic("alice", f"fact number {i}", topic=f"topic{i}")


def test_no_cap_keeps_all():
    os.environ.pop("ADK_CC_MEMORY_EPISODIC_CAP", None)
    s = _store()
    _seed(s, 5)
    rep = consolidate_user(s, "alice")
    assert rep.pruned_episodic == 0
    assert len(s.list_episodic("alice", status=CONSOLIDATED)) == 5
    assert len(s.list_episodic("alice", status=ARCHIVED)) == 0


def test_cap_archives_oldest_beyond_cap():
    os.environ["ADK_CC_MEMORY_EPISODIC_CAP"] = "2"
    try:
        s = _store()
        _seed(s, 5)
        rep = consolidate_user(s, "alice")
        assert rep.pruned_episodic == 3, rep
        assert len(s.list_episodic("alice", status=CONSOLIDATED)) == 2
        assert len(s.list_episodic("alice", status=ARCHIVED)) == 3
        # prune is logged (Fix G) and reversible
        ops = [e["op"] for e in s.read_changelog("alice")]
        assert ops.count(f"status:{ARCHIVED}") == 3, ops
    finally:
        os.environ.pop("ADK_CC_MEMORY_EPISODIC_CAP", None)


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
    print("\nall prune tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
