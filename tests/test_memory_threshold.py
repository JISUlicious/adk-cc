"""Tests for the hybrid's threshold-triggered promotion (capture-path half).

Model-free: exercises maybe_threshold_consolidate + pending_episodic_count
directly, so it's fast and needs no live model.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.memory import MemoryStore, pending_episodic_count
from adk_cc.plugins.memory import maybe_threshold_consolidate


def _store() -> MemoryStore:
    root = tempfile.mkdtemp(prefix="thr-mem-")
    return MemoryStore.for_tenant("acme", root=root)


def test_pending_count_tracks_unconsolidated():
    s = _store()
    assert pending_episodic_count(s, "alice") == 0
    s.add_episodic("alice", "fact one", topic="t1")
    s.add_episodic("alice", "fact two", topic="t2")
    assert pending_episodic_count(s, "alice") == 2


def test_below_threshold_does_not_promote():
    os.environ["ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD"] = "3"
    s = _store()
    s.add_episodic("alice", "deploys to fly", topic="deploy")
    s.add_episodic("alice", "uses postgres", topic="db")
    rep = asyncio.run(maybe_threshold_consolidate(s, "alice"))
    assert rep is None, "should not fire below threshold"
    assert s.list_semantic("alice") == []
    assert pending_episodic_count(s, "alice") == 2


def test_at_threshold_promotes_and_resets():
    os.environ["ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD"] = "3"
    s = _store()
    s.add_episodic("alice", "deploys to fly", topic="deploy")
    s.add_episodic("alice", "uses postgres", topic="db")
    s.add_episodic("alice", "prefers dark mode", topic="prefs")
    rep = asyncio.run(maybe_threshold_consolidate(s, "alice"))
    assert rep is not None and rep.topics_consolidated == 3, rep
    sem = {i.topic for i in s.list_semantic("alice")}
    assert sem == {"deploy", "db", "prefs"}, sem
    # sources flipped to CONSOLIDATED → pending resets to 0
    assert pending_episodic_count(s, "alice") == 0


def test_llm_synth_processes_semantic_text():
    # with a model + SYNTH != deterministic, the threshold consolidation should
    # REWRITE the semantic text (not copy the episodic verbatim).
    from types import SimpleNamespace

    class _Model:
        async def generate_content_async(self, req, stream=False):
            yield SimpleNamespace(content=SimpleNamespace(
                parts=[SimpleNamespace(text="DISTILLED: user deploys to Fly.io", thought=False)]))

    os.environ["ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD"] = "2"
    os.environ.pop("ADK_CC_MEMORY_SYNTH", None)  # default → LLM when model given
    try:
        s = _store()
        s.add_episodic("alice", "I deploy to fly", topic="deploy")
        s.add_episodic("alice", "deploying on fly.io", topic="deploy")
        rep = asyncio.run(maybe_threshold_consolidate(s, "alice", model=_Model()))
        assert rep is not None and rep.topics_consolidated == 1, rep
        sem = s.list_semantic("alice")[0]
        assert sem.text.startswith("DISTILLED:"), sem.text  # rewritten, not verbatim
    finally:
        os.environ.pop("ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD", None)


def test_deterministic_when_synth_env_set():
    os.environ["ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD"] = "2"
    os.environ["ADK_CC_MEMORY_SYNTH"] = "deterministic"
    try:
        s = _store()
        s.add_episodic("alice", "first", topic="t")
        s.add_episodic("alice", "second latest", topic="t")
        asyncio.run(maybe_threshold_consolidate(s, "alice", model=object()))
        sem = s.list_semantic("alice")[0]
        # deterministic = a VERBATIM episodic (not LLM-rewritten); which of the
        # two wins is timestamp-order dependent, so accept either.
        assert sem.text in ("first", "second latest"), sem.text
    finally:
        os.environ.pop("ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD", None)
        os.environ.pop("ADK_CC_MEMORY_SYNTH", None)


def test_disabled_when_threshold_zero():
    os.environ["ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD"] = "0"
    s = _store()
    for i in range(5):
        s.add_episodic("alice", f"fact {i}", topic=f"t{i}")
    rep = asyncio.run(maybe_threshold_consolidate(s, "alice"))
    assert rep is None, "threshold=0 must disable the trigger"
    assert s.list_semantic("alice") == []


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
        finally:
            os.environ.pop("ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD", None)
    print("\nall threshold tests passed" if not failed else f"\n{failed} test(s) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
