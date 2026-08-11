"""#129-4: memory topic dedup — canonical consolidation + capture hints.

THE task-bar A/B: episodics under 'preferred-language',
'user-preferred-language' and 'language-preference' carrying the same fact
must consolidate into ONE active semantic node; the same run with
ADK_CC_MEMORY_CANONICALIZE=0 produces three (the old behavior).

Also: variants fold onto an EXISTING semantic node instead of minting a
sibling; already-grown duplicate nodes merge (newest wins, history kept,
variants archived); the threshold trigger corroborates across variants;
the capture prompt carries known topics.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_memory_dedup.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
for _k in ("ADK_CC_MEMORY_CANONICALIZE", "ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD",
           "ADK_CC_MEMORY_EPISODIC_CAP"):
    os.environ.pop(_k, None)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


VARIANTS = ["preferred-language", "user-preferred-language",
            "language-preference"]
FACT = "The user prefers answers in Korean."


def _store(tmp):
    os.environ["ADK_CC_MEMORY_ROOT"] = tmp
    from adk_cc.memory.store import MemoryStore

    return MemoryStore.for_tenant("local")


def _active_semantic(store, user="u1"):
    from adk_cc.memory.store import ACTIVE, CONSOLIDATED

    return [s for s in store.list_semantic(user)
            if s.status in (ACTIVE, CONSOLIDATED)]


def main() -> int:
    from adk_cc.memory.consolidate import consolidate_user

    # ---- THE task-bar A/B ------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        for t in VARIANTS:
            st.add_episodic("u1", FACT, topic=t)
        rep = consolidate_user(st, "u1")
        sems = _active_semantic(st)
        check("A: three slug variants consolidate into ONE semantic node",
              len(sems) == 1, [s.topic for s in sems])
        check("A: the node carries the fact", sems and FACT in sems[0].text)

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ADK_CC_MEMORY_CANONICALIZE"] = "0"
        try:
            st = _store(tmp)
            for t in VARIANTS:
                st.add_episodic("u1", FACT, topic=t)
            consolidate_user(st, "u1")
            sems = _active_semantic(st)
            check("B (control): canonicalize off → three separate nodes",
                  len(sems) == 3, [s.topic for s in sems])
        finally:
            os.environ.pop("ADK_CC_MEMORY_CANONICALIZE", None)

    # ---- variants fold onto the EXISTING semantic node -------------------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.add_episodic("u1", "The user prefers English.",
                        topic="user-preferred-language")
        consolidate_user(st, "u1", topic_canonicalizer=lambda ts: {})
        assert len(_active_semantic(st)) == 1
        st.add_episodic("u1", FACT, topic="preferred-language")
        consolidate_user(st, "u1")
        sems = _active_semantic(st)
        check("existing node wins as the representative",
              len(sems) == 1 and sems[0].topic == "user-preferred-language",
              [s.topic for s in sems])
        check("update lands on it with history",
              FACT in sems[0].text
              and any("English" in s for s in sems[0].supersedes))

    # ---- duplicate nodes that already grew get merged --------------------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        # simulate the pre-fix pollution: two variant nodes grown at
        # DIFFERENT times (explicit epochs — the store's timestamps are
        # second-resolution, and back-to-back runs tie)
        st.add_episodic("u1", "The user prefers English.",
                        topic="preferred-language")
        consolidate_user(st, "u1", topic_canonicalizer=lambda ts: {},
                         now_epoch=1_000_000)
        st.add_episodic("u1", FACT, topic="user-preferred-language")
        consolidate_user(st, "u1", topic_canonicalizer=lambda ts: {},
                         now_epoch=2_000_000)
        assert len(_active_semantic(st)) == 2
        rep = consolidate_user(st, "u1")  # canonicalizer on, nothing fresh
        sems = _active_semantic(st)
        check("dup sweep: one survivor", len(sems) == 1, [s.topic for s in sems])
        check("dup sweep: newest value is current, older in history",
              FACT in sems[0].text
              and any("English" in s for s in sems[0].supersedes),
              (sems[0].text, sems[0].supersedes) if sems else None)
        check("dup sweep: counted in the report",
              rep.duplicates_merged == 1, rep.duplicates_merged)

    # ---- threshold trigger corroborates across variants ------------------
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD"] = "2"
        try:
            from adk_cc.plugins.memory import maybe_threshold_consolidate

            st = _store(tmp)
            st.add_episodic("u1", FACT, topic="preferred-language")
            st.add_episodic("u1", FACT + " Always.",
                            topic="user-preferred-language")
            rep = asyncio.run(maybe_threshold_consolidate(st, "u1", model=None))
            check("threshold: variant slugs corroborate to reach the bar",
                  rep is not None and rep.topics_consolidated == 1
                  and len(_active_semantic(st)) == 1,
                  getattr(rep, "topics", rep))
        finally:
            os.environ.pop("ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD", None)

    # ---- capture prompt hint --------------------------------------------
    from adk_cc.plugins.memory import _capture_prompt, _known_topics

    with_hint = _capture_prompt("turn text", ["preferred-language", "editor"])
    without = _capture_prompt("turn text")
    check("capture prompt lists known topics + reuse instruction",
          "KNOWN TOPICS: preferred-language, editor" in with_hint
          and "REUSE that exact slug" in with_hint
          and "KNOWN TOPICS" not in without)

    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.add_episodic("u1", FACT, topic="preferred-language")
        consolidate_user(st, "u1", topic_canonicalizer=lambda ts: {})
        st.add_episodic("u1", "User uses vim.", topic="editor")
        topics = _known_topics(st, "u1")
        check("_known_topics: semantic first, then pending episodic",
              topics == ["preferred-language", "editor"], topics)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
