"""#129-3: personal LLM-wiki pass — the librarian core against users/<uid>/wiki.

The load-bearing properties: the SAME merge engine runs against a personal
target (no second conflict machinery); the personal pass NEVER interferes
with the domain pass (marker-based consumption, the inbox doc stays put);
per-user isolation (u1's pass never touches u2 or the domain); personal
adjudications are KV-namespaced away from domain ones; and search/read
surface personal pages tagged with their scope.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_personal_wiki.py
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


def _run(view):
    from adk_cc.wiki import Librarian, conflict
    from adk_cc.wiki.conflict import Verdict

    def classify(claim, page):
        return Verdict(conflict.NOVEL, reason="new fact")

    return asyncio.run(Librarian(view, classifier=classify).run())


def main() -> int:
    from adk_cc.wiki import Librarian, PersonalWikiView

    # ---- personal pass consolidates the user's notes -----------------------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.add_inbox("u1", "GPT-4 Turbo has a 128K window.", topic="gpt-4-turbo")
        st.add_inbox("u1", "Fly.io deploys need a Dockerfile.", topic="fly-io")
        st.add_inbox("u2", "Postgres 16 supports MERGE.", topic="postgres")

        view = PersonalWikiView(st, "u1")
        rep = _run(view)
        check("u1 personal pass publishes u1's notes",
              rep.claims_seen == 2 and set(view.list_domain_pages())
              == {"gpt-4-turbo", "fly-io"}, (rep.claims_seen, view.list_domain_pages()))
        page = view.read_domain_page("gpt-4-turbo")
        check("personal page is scope-tagged",
              page and page.frontmatter.get("scope") == "personal")
        check("domain wiki untouched", st.list_domain_pages() == [])
        check("u2 untouched",
              PersonalWikiView(st, "u2").list_domain_pages() == [])

        # non-interference: the inbox doc is still there for the domain pass
        check("inbox docs NOT consumed (marker, not move)",
              len(st.list_inbox("u1")) == 2)
        # idempotency: a second personal run sees nothing new
        rep2 = _run(view)
        check("personal pass is idempotent (markers)", rep2.claims_seen == 0,
              rep2.claims_seen)

    # ---- domain-merged docs still feed the personal wiki -------------------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.add_inbox("u1", "GPT-4 Turbo has a 128K window.", topic="gpt-4-turbo")
        # domain librarian consumes the inbox FIRST (archives to merged/)
        from adk_cc.wiki import conflict
        from adk_cc.wiki.conflict import Verdict

        asyncio.run(Librarian(
            st, classifier=lambda c, p: Verdict(conflict.NOVEL)).run())
        assert st.list_inbox("u1") == [] and st.list_merged("u1")
        view = PersonalWikiView(st, "u1")
        rep = _run(view)
        check("domain-merged docs still reach the personal pass",
              rep.claims_seen == 1
              and view.list_domain_pages() == ["gpt-4-turbo"],
              (rep.claims_seen, view.list_domain_pages()))

    # ---- KV namespacing: personal sticky never collides with domain --------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        view = PersonalWikiView(st, "u1")
        view.set_sticky("h1", action="reject", by="human")
        check("personal sticky invisible to the domain store",
              st.get_sticky("h1") is None and view.get_sticky("h1") is not None)

    # ---- search + read tag the personal scope ------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.add_inbox("u1", "GPT-4 Turbo has a 128K window.", topic="gpt-4-turbo")
        view = PersonalWikiView(st, "u1")
        _run(view)
        # consume the inbox note so only the personal PAGE carries the fact
        from adk_cc.wiki import conflict
        from adk_cc.wiki.conflict import Verdict

        asyncio.run(Librarian(
            st, classifier=lambda c, p: Verdict(conflict.NOVEL)).run())
        from adk_cc.wiki import search as searchlib

        hits = searchlib.search(st, "128K window", user_id="u1")
        check("search surfaces the personal page tagged scope=personal",
              any(h.scope == "personal" and h.slug == "gpt-4-turbo"
                  for h in hits), [(h.slug, h.scope) for h in hits])
        hits_anon = searchlib.search(st, "128K window")
        check("no user → personal pages NOT searched",
              not any(h.scope == "personal" for h in hits_anon))

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
