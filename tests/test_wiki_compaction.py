"""Wiki ports from memory: merge-verify gate (Fix D) + domain compaction (Fix F).
Model-free — fake resolver/verifier stand in for the LLM.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.wiki import Librarian, WikiStore
from adk_cc.wiki.page import Page


def _store():
    return WikiStore.for_tenant("acme", root=tempfile.mkdtemp(prefix="wcmp-")).ensure()


def _page(slug, body, sources):
    return Page(slug=slug, frontmatter={"title": slug, "sources": sources}, body=body + "\n")


async def _yes(a, b):
    return True


async def _no(a, b):
    return False


# resolver that forces 'b-thing' to merge into the existing 'a-thing'
def _force_merge(slug, known):
    return "a-thing" if slug == "b-thing" else slug


# ---------- Fix D: merge-verify gate in the merge run ----------
def test_verify_gate_rejects_bad_entity_merge():
    s = _store()
    s.write_domain_page(_page("a-thing", "A-thing is about cats.", ["s0"]))
    s.add_inbox("bob", "B-thing is about dogs.", topic="b thing", sources=["s1"])
    lib = Librarian(s, resolver=_force_merge, verifier=_no)
    rep = asyncio.run(lib.run())
    assert rep.actions.get("merge_rejected") == 1, rep.actions  # gate fired


def test_verify_gate_allows_good_entity_merge():
    s = _store()
    s.write_domain_page(_page("a-thing", "A-thing is about cats.", ["s0"]))
    s.add_inbox("bob", "More about a-thing.", topic="b thing", sources=["s1"])
    lib = Librarian(s, resolver=_force_merge, verifier=_yes)
    rep = asyncio.run(lib.run())
    assert not rep.actions.get("merge_rejected"), rep.actions  # merge allowed


def test_no_verifier_keeps_current_behavior():
    s = _store()
    s.write_domain_page(_page("a-thing", "A-thing is about cats.", ["s0"]))
    s.add_inbox("bob", "B-thing.", topic="b thing", sources=["s1"])
    lib = Librarian(s, resolver=_force_merge)  # verifier=None → trust resolver
    rep = asyncio.run(lib.run())
    assert not rep.actions.get("merge_rejected"), rep.actions


# ---------- Fix F: domain compaction ----------
def test_compaction_merges_hyphenation_duplicates_modelfree():
    s = _store()
    s.write_domain_page(_page("gpt-4", "GPT-4 is a model.", ["s1"]))
    s.write_domain_page(_page("gpt4", "GPT4 has 8k context.", ["s2"]))
    lib = Librarian(s)  # deterministic _canonicalize resolver, no verifier
    rep = asyncio.run(lib.compact())
    assert rep.pages_before == 2 and rep.pages_after == 1, rep
    assert rep.merged == 1 and rep.groups == 1, rep
    survivor = s.read_domain_page("gpt-4")
    assert survivor is not None
    assert "8k context" in survivor.body                    # loser's facts folded in
    assert set(survivor.frontmatter.get("sources")) == {"s1", "s2"}  # sources unioned
    assert s.read_domain_page("gpt4") is None               # loser removed
    ops = [e.get("op") for e in _changelog(s)]
    assert "compact_merge" in ops


def test_compaction_verify_rejects_keeps_pages_separate():
    s = _store()
    s.write_domain_page(_page("gpt-4", "GPT-4 is a model.", ["s1"]))
    s.write_domain_page(_page("gpt4", "GPT4 has 8k context.", ["s2"]))
    lib = Librarian(s, verifier=_no)  # reject every merge
    rep = asyncio.run(lib.compact())
    assert rep.merged == 0 and rep.pages_after == 2, rep
    assert s.read_domain_page("gpt4") is not None
    assert rep.actions.get("merge_rejected") == 1, rep.actions


def test_compaction_noop_when_no_duplicates():
    s = _store()
    s.write_domain_page(_page("cats", "About cats.", ["s1"]))
    s.write_domain_page(_page("dogs", "About dogs.", ["s2"]))
    lib = Librarian(s)
    rep = asyncio.run(lib.compact())
    assert rep.merged == 0 and rep.pages_after == 2, rep


def _changelog(store) -> list:
    import json
    raw = store.read_changelog()
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


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
    print("\nall wiki compaction/verify tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
