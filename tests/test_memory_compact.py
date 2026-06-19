"""Fix F tests: periodic LLM compaction merges residual duplicate topics.

Uses a prompt-aware fake model (no live endpoint): it answers the canon-grouping
prompt by lumping the two fragmented topics, and the verify prompt with YES.
"""

from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.memory import MemoryStore, compact_user
from adk_cc.memory.store import ACTIVE, ARCHIVED, MemoryItem, SEMANTIC


class _FakeModel:
    """Groups the two known fragments under one canonical; says YES to verify."""

    async def generate_content_async(self, req, stream=False):
        prompt = req.contents[0].parts[0].text
        if "Answer EXACTLY" in prompt:          # verify prompt
            text = "YES"
        else:                                    # canon-grouping prompt
            text = "user-profession: user-profession, user-focus"
        yield SimpleNamespace(content=SimpleNamespace(
            parts=[SimpleNamespace(text=text, thought=False)]))


def _store():
    return MemoryStore.for_tenant("acme", root=tempfile.mkdtemp(prefix="cmp-mem-"))


def test_compaction_merges_fragments():
    s = _store()
    s.put_semantic("alice", MemoryItem(id="user-profession", topic="user-profession",
                                       text="designs embedded low-power cores", confidence=0.5))
    s.put_semantic("alice", MemoryItem(id="user-focus", topic="user-focus",
                                       text="focus is embedded low-power cores", confidence=0.5))
    assert len(s.list_semantic("alice", status=ACTIVE)) == 2

    rep = compact_user(_FakeModel(), s, "alice")
    assert rep["merged"] == 1 and rep["groups"] == 1, rep
    active = s.list_semantic("alice", status=ACTIVE)
    assert len(active) == 1, [a.topic for a in active]
    survivor = active[0]
    assert survivor.topic == "user-profession"
    # merged-away value preserved in supersedes; confidence bumped
    assert "focus is embedded low-power cores" in survivor.supersedes, survivor.supersedes
    assert survivor.confidence > 0.5
    # the loser is archived (reversible) and logged
    assert len(s.list_semantic("alice", status=ARCHIVED)) == 1
    assert any(e["op"] == f"status:{ARCHIVED}" for e in s.read_changelog("alice"))


def test_compaction_noop_without_model():
    s = _store()
    s.put_semantic("alice", MemoryItem(id="a", topic="a", text="x"))
    s.put_semantic("alice", MemoryItem(id="b", topic="b", text="y"))
    assert compact_user(None, s, "alice") == {"merged": 0, "groups": 0}
    assert len(s.list_semantic("alice", status=ACTIVE)) == 2


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
    print("\nall compaction tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
