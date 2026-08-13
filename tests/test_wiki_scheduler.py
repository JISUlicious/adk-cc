"""#130 P1: in-process wiki-librarian scheduler.

Model-free at unit level (injected classifier per house rule; the live
model path is the battery's job): the lifespan loop merges an inbox note
into a domain page with no external cron; interval unset → no task; a
raising tick doesn't kill the loop; MODEL=0 builds no model stack.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_wiki_scheduler.py
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
for _k in list(os.environ):
    if _k.startswith("ADK_CC_WIKI_LIBRARIAN") or _k in (
            "ADK_CC_WIKI", "ADK_CC_WIKI_ROOT", "ADK_CC_PERSONAL_WIKI"):
        os.environ.pop(_k)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    from adk_cc.service import wiki_scheduler as ws
    from adk_cc.wiki import WikiStore, conflict
    from adk_cc.wiki.conflict import Verdict

    def classify(claim, page):
        return Verdict(conflict.NOVEL, reason="new")

    # ---- enablement gate -------------------------------------------------
    check("disabled: no wiki flag", not ws.scheduler_enabled())
    os.environ["ADK_CC_WIKI"] = "1"
    check("disabled: wiki on but no interval", not ws.scheduler_enabled())
    os.environ["ADK_CC_WIKI_LIBRARIAN_INTERVAL_S"] = "900"
    check("enabled: wiki + interval", ws.scheduler_enabled())

    # ---- MODEL=0 builds no stack ----------------------------------------
    os.environ["ADK_CC_WIKI_LIBRARIAN_MODEL"] = "0"
    c, s = ws.make_librarian_stack()
    check("MODEL=0: heuristic-only stack", c is None and s is None)
    os.environ.pop("ADK_CC_WIKI_LIBRARIAN_MODEL", None)

    # ---- run_librarian_once merges (injected classifier) -----------------
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ADK_CC_WIKI_ROOT"] = tmp
        st = WikiStore.for_tenant("local").ensure()
        st.add_inbox("u1", "GPT-4 Turbo has a 128K window.",
                     topic="gpt-4-turbo")
        reports = asyncio.run(ws.run_librarian_once(
            tmp, personal=True, classifier=classify))
        check("run_once: inbox note becomes a domain page",
              st.read_domain_page("gpt-4-turbo") is not None)
        check("run_once: personal pass ran too",
              any(getattr(r, "claims_seen", 0) for r in reports[1:])
              or len(reports) >= 2, [r.claims_seen for r in reports])
        check("run_once: never raises on a bad root",
              asyncio.run(ws.run_librarian_once(
                  "/nonexistent-root-xyz", personal=False)) == [])

    # ---- the LIFESPAN loop end to end ------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.update({
            "ADK_CC_WIKI_ROOT": tmp,
            "ADK_CC_WIKI_LIBRARIAN_INTERVAL_S": "0.2",
            "ADK_CC_WIKI_LIBRARIAN_DELAY_S": "0",
            "ADK_CC_WIKI_LIBRARIAN_MODEL": "0",  # loop itself: heuristic
        })
        st = WikiStore.for_tenant("local").ensure()
        st.add_inbox("u1", "Fly.io deploys need a Dockerfile.",
                     topic="fly-io")

        async def drive():
            import contextlib as _cl

            @_cl.asynccontextmanager
            async def inner(app):
                yield

            async with ws.make_wiki_lifespan(inner)(None):
                for _ in range(40):
                    await asyncio.sleep(0.1)
                    if st.read_domain_page("fly-io") is not None:
                        return True
            return False

        check("lifespan loop: note published with NO external cron",
              asyncio.run(drive()))

        # a raising tick must not kill the loop
        calls = {"n": 0}
        real_once = ws.run_librarian_once

        async def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return await real_once(*a, **kw)

        ws.run_librarian_once = flaky
        try:
            async def drive2():
                import contextlib as _cl

                @_cl.asynccontextmanager
                async def inner(app):
                    yield

                async with ws.make_wiki_lifespan(inner)(None):
                    for _ in range(30):
                        await asyncio.sleep(0.1)
                        if calls["n"] >= 2:
                            return True
                return False

            check("a raising tick doesn't kill the loop",
                  asyncio.run(drive2()), calls)
        finally:
            ws.run_librarian_once = real_once

    # ---- #130 P2: wiki_add threshold trigger -----------------------------
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.update({"ADK_CC_WIKI_ROOT": tmp, "ADK_CC_WIKI": "1",
                           "ADK_CC_WIKI_LIBRARIAN_MODEL": "0"})
        os.environ.pop("ADK_CC_WIKI_LIBRARIAN_THRESHOLD", None)
        st = WikiStore.for_tenant("local").ensure()
        st.add_inbox("u1", "Fact one.", topic="fact-one")

        async def no_thr():
            return ws.maybe_trigger_librarian("local")

        check("threshold unset: no trigger", not asyncio.run(no_thr()))

        os.environ["ADK_CC_WIKI_LIBRARIAN_THRESHOLD"] = "1"
        ws._DEBOUNCE_S = 0.05

        async def trig():
            first = ws.maybe_trigger_librarian("local")
            burst = ws.maybe_trigger_librarian("local")  # one in-flight
            for _ in range(60):
                await asyncio.sleep(0.05)
                if st.read_domain_page("fact-one") is not None:
                    return first, burst, True
            return first, burst, False

        first, burst, published = asyncio.run(trig())
        check("threshold=1: trigger scheduled", first)
        check("burst: second add coalesces into the in-flight run", not burst)
        check("triggered run publishes the note (heuristic path)", published)
        os.environ.pop("ADK_CC_WIKI_LIBRARIAN_THRESHOLD", None)

    # ---- interval unset → lifespan starts no task ------------------------
    os.environ.pop("ADK_CC_WIKI_LIBRARIAN_INTERVAL_S", None)

    async def drive3():
        import contextlib as _cl

        @_cl.asynccontextmanager
        async def inner(app):
            yield

        async with ws.make_wiki_lifespan(inner)(None):
            names = {t.get_name() for t in asyncio.all_tasks()}
            return "adk_cc_wiki_librarian" not in names

    check("interval unset: no librarian task started", asyncio.run(drive3()))

    for k in list(os.environ):
        if k.startswith("ADK_CC_WIKI"):
            os.environ.pop(k)
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
