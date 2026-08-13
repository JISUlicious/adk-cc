"""#130 P0: the librarian's single-writer lock.

Until now single-writer was cron DISCIPLINE, not enforcement — two
overlapping runs raced on the shared domain pages. Every entry point
(run, compact, personal pass) now takes a non-blocking per-tenant flock;
the loser skips (skipped_locked) and retries at its next tick.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_wiki_librarian_lock.py
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

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _store(tmp):
    os.environ["ADK_CC_WIKI_ROOT"] = tmp
    from adk_cc.wiki import WikiStore

    return WikiStore.for_tenant("local").ensure()


def main() -> int:
    from adk_cc.wiki import Librarian, PersonalWikiView, conflict
    from adk_cc.wiki.conflict import Verdict
    from adk_cc.wiki.librarian import _LibrarianLock

    def classify(claim, page):
        return Verdict(conflict.NOVEL, reason="new")

    # ---- concurrent runs: exactly one merges ----------------------------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.add_inbox("u1", "GPT-4 Turbo has a 128K window.", topic="gpt-4-turbo")

        async def slow_classify(claim, page):
            await asyncio.sleep(0.3)  # hold the lock across a real yield
            return Verdict(conflict.NOVEL, reason="new")

        async def race():
            a = Librarian(st, classifier=slow_classify)
            b = Librarian(st, classifier=slow_classify)
            r1, r2 = await asyncio.gather(a.run(), b.run())
            return r1, r2

        r1, r2 = asyncio.run(race())
        winners = [r for r in (r1, r2) if not r.skipped_locked]
        losers = [r for r in (r1, r2) if r.skipped_locked]
        check("concurrent runs: exactly one merges, one skips",
              len(winners) == 1 and len(losers) == 1,
              (r1.skipped_locked, r2.skipped_locked))
        check("winner published the page",
              st.read_domain_page("gpt-4-turbo") is not None)
        check("loser did no work", losers and losers[0].claims_seen == 0)
        check("lock file lives under the tenant root",
              os.path.isfile(os.path.join(tmp, "local", ".librarian.lock")))

    # ---- externally-held lock: run + compact both skip -------------------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.add_inbox("u1", "Fact.", topic="fact")
        held = _LibrarianLock(st)
        assert held.acquire()
        try:
            r = asyncio.run(Librarian(st, classifier=classify).run())
            check("held lock: run() skips without writing",
                  r.skipped_locked and st.read_domain_page("fact") is None)
            c = asyncio.run(Librarian(st).compact())
            check("held lock: compact() skips", c.skipped_locked)
            # the PERSONAL pass shares the tenant lock (same store base)
            p = asyncio.run(Librarian(PersonalWikiView(st, "u1"),
                                      classifier=classify).run())
            check("held lock: personal pass shares the tenant lock",
                  p.skipped_locked)
        finally:
            held.release()
        r = asyncio.run(Librarian(st, classifier=classify).run())
        check("after release: run() proceeds",
              not r.skipped_locked
              and st.read_domain_page("fact") is not None)

    # ---- released on exception ------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.add_inbox("u1", "Fact.", topic="fact")
        lib = Librarian(st, classifier=classify)
        lib._lint = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            asyncio.run(lib.run())
            check("exception propagates out of run()", False)
        except RuntimeError:
            check("exception propagates out of run()", True)
        r = asyncio.run(Librarian(st, classifier=classify).run())
        check("lock released after the exception (next run works)",
              not r.skipped_locked)

    # ---- non-file store fallback (threading lock) ------------------------
    class _Stub:
        tenant_id = "t1"
        store = object()  # no .base

    la, lb = _LibrarianLock(_Stub()), _LibrarianLock(_Stub())
    check("fallback: first acquire wins", la.acquire())
    check("fallback: second acquire refused", not lb.acquire())
    la.release()
    check("fallback: reacquire after release", lb.acquire())
    lb.release()

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
