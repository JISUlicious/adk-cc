"""Tests for the conflict policy + librarian merge pipeline (Phase 3).

Two layers:
  - conflict.resolve(...) — PURE decision policy: classification→action,
    auto-supersede, corroboration threshold, cite-or-quarantine, sticky
    human overrides. No IO, no model.
  - Librarian.run() — the full pipeline driven by a FAKE deterministic
    classifier, so every merge path (novel add, supersede, corroborated
    overturn, contested, uncited-quarantine, reject, no-promote skip,
    idempotency) is exercised without a model. The live model is covered
    by tests/e2e_wiki_merge.py.

Hand-rolled.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.memory import conflict
from adk_cc.memory.conflict import ClaimRecord, Verdict
from adk_cc.memory.librarian import Librarian
from adk_cc.memory.page import Page
from adk_cc.memory.store import WikiStore


def _claim(slug="x", text="a fact", user="alice", sources=None):
    return ClaimRecord(slug=slug, text=text, user_id=user, doc_id="d1",
                       sources=sources or [])


# ----------------------- pure policy: conflict.resolve -----------------------
def test_resolve_auto_tier():
    assert conflict.resolve(Verdict(conflict.NOVEL), _claim()).action == conflict.ADD
    assert conflict.resolve(Verdict(conflict.AGREES), _claim()).action == conflict.CORROBORATE
    assert conflict.resolve(Verdict(conflict.REFINEMENT), _claim()).action == conflict.REFINE
    # operator chose AUTO-SUPERSEDE for time-ordered claims
    assert conflict.resolve(Verdict(conflict.SUPERSESSION), _claim()).action == conflict.SUPERSEDE
    assert conflict.resolve(Verdict(conflict.ERROR), _claim()).action == conflict.REJECT
    print("OK resolve_auto_tier")


def test_resolve_contradiction_threshold():
    v = Verdict(conflict.CONTRADICTION)
    # below threshold → CONTEST (record both, queue)
    r = conflict.resolve(v, _claim(), support_count=1, corroboration_n=2)
    assert r.action == conflict.CONTEST, r
    # at threshold WITH external source → OVERTURN
    r2 = conflict.resolve(v, _claim(sources=["paper-1"]), support_count=2, corroboration_n=2)
    assert r2.action == conflict.OVERTURN, r2
    # at threshold but UNCITED → QUARANTINE (cite-or-quarantine defense)
    r3 = conflict.resolve(v, _claim(sources=[]), support_count=2, corroboration_n=2)
    assert r3.action == conflict.QUARANTINE, r3
    print("OK resolve_contradiction_threshold")


def test_resolve_human_override_wins():
    v = Verdict(conflict.CONTRADICTION)
    # human reject beats everything, even a corroborated+cited overturn
    r = conflict.resolve(v, _claim(sources=["p"]), support_count=9, corroboration_n=2,
                         human_override="reject")
    assert r.action == conflict.REJECT and "human" in r.reason
    # human accept publishes despite low support / no source
    r2 = conflict.resolve(v, _claim(), support_count=0, human_override="accept")
    assert r2.action == conflict.ADD and "human" in r2.reason
    # human accept on a supersession publishes as SUPERSEDE
    r3 = conflict.resolve(Verdict(conflict.SUPERSESSION), _claim(), human_override="accept")
    assert r3.action == conflict.SUPERSEDE
    print("OK resolve_human_override_wins")


def test_claim_hash_stable_and_normalized():
    a = conflict.claim_hash("gpt-4", "Context is 128k tokens.")
    b = conflict.claim_hash("gpt-4", "  context   IS 128K   tokens.  ")
    c = conflict.claim_hash("gpt-4", "Context is 8k tokens.")
    assert a == b, "whitespace/case normalization should give a stable hash"
    assert a != c
    print("OK claim_hash_stable_and_normalized")


# ----------------------- pipeline: Librarian.run -----------------------
def _fixed(classification):
    return lambda claim, page: Verdict(classification, "fixed")


def _by_slug(mapping):
    """classifier that returns a verdict per slug (else AGREES)."""
    return lambda claim, page: Verdict(mapping.get(claim.slug, conflict.AGREES), "by-slug")


def test_pipeline_novel_add_and_archive():
    with tempfile.TemporaryDirectory() as root:
        st = WikiStore.for_tenant("acme", root=root).ensure()
        st.add_inbox("alice", "Acme's API base URL is api.acme.com.", topic="acme-api")
        lib = Librarian(st, classifier=_fixed(conflict.NOVEL))
        report = asyncio.run(lib.run())
        # domain page created, index rebuilt, inbox archived (copy in merged/)
        page = st.read_domain_page("acme-api")
        assert page is not None and "api.acme.com" in page.body
        assert "acme-api" in st.read_index()
        assert st.list_inbox("alice") == []
        assert os.path.isdir(st.merged_dir("alice"))
        assert report.actions.get(conflict.ADD) == 1
        print("OK pipeline_novel_add_and_archive")


def test_pipeline_supersede_appends_validity():
    with tempfile.TemporaryDirectory() as root:
        st = WikiStore.for_tenant("acme", root=root).ensure()
        st.write_domain_page(Page("gpt-4", {"title": "GPT-4"}, "Context window is 8k tokens.\n"))
        st.add_inbox("alice", "Context window is now 128k tokens.", topic="gpt-4")
        lib = Librarian(st, classifier=_fixed(conflict.SUPERSESSION))
        asyncio.run(lib.run())
        page = st.read_domain_page("gpt-4")
        assert "128k" in page.body
        assert page.frontmatter.get("validity"), "supersession must append a validity window"
        print("OK pipeline_supersede_appends_validity")


def test_pipeline_corroborated_overturn_vs_contest():
    # two users contradict domain WITH sources, N=2 → OVERTURN
    with tempfile.TemporaryDirectory() as root:
        st = WikiStore.for_tenant("acme", root=root).ensure()
        st.set_setting("corroboration_n", 2)
        st.write_domain_page(Page("fact", {}, "The sky is green.\n"))
        st.add_inbox("alice", "The sky is blue.", topic="fact", sources=["obs-1"])
        st.add_inbox("bob", "The sky is blue.", topic="fact", sources=["obs-2"])
        lib = Librarian(st, classifier=_fixed(conflict.CONTRADICTION))
        report = asyncio.run(lib.run())
        assert report.actions.get(conflict.OVERTURN) == 2, report.actions
        assert "blue" in st.read_domain_page("fact").body
    # single user contradiction → CONTEST (page contested + queued), no overturn
    with tempfile.TemporaryDirectory() as root:
        st = WikiStore.for_tenant("acme", root=root).ensure()
        st.set_setting("corroboration_n", 2)
        st.write_domain_page(Page("fact", {}, "The sky is green.\n"))
        st.add_inbox("alice", "The sky is blue.", topic="fact", sources=["obs-1"])
        lib = Librarian(st, classifier=_fixed(conflict.CONTRADICTION))
        report = asyncio.run(lib.run())
        assert report.actions.get(conflict.CONTEST) == 1, report.actions
        page = st.read_domain_page("fact")
        assert page.contested is True
        assert "green" in page.body and "blue" in page.body  # both sides recorded
        assert len(st.list_quarantine()) == 1  # queued for human adjudication
        print("OK pipeline_corroborated_overturn_vs_contest")


def test_pipeline_uncited_overturn_quarantines_and_holds_inbox():
    with tempfile.TemporaryDirectory() as root:
        st = WikiStore.for_tenant("acme", root=root).ensure()
        st.set_setting("corroboration_n", 2)
        st.write_domain_page(Page("fact", {}, "The sky is green.\n"))
        # two corroborating users but NO external source → cite-or-quarantine
        st.add_inbox("alice", "The sky is blue.", topic="fact")
        st.add_inbox("bob", "The sky is blue.", topic="fact")
        lib = Librarian(st, classifier=_fixed(conflict.CONTRADICTION))
        report = asyncio.run(lib.run())
        assert report.actions.get(conflict.QUARANTINE) == 2, report.actions
        # domain unchanged; inbox docs held (not archived)
        assert "green" in st.read_domain_page("fact").body
        assert "blue" not in st.read_domain_page("fact").body
        assert len(st.list_inbox("alice")) == 1
        assert len(st.list_quarantine()) >= 1
        print("OK pipeline_uncited_overturn_quarantines_and_holds_inbox")


def test_pipeline_no_promote_is_skipped():
    with tempfile.TemporaryDirectory() as root:
        st = WikiStore.for_tenant("acme", root=root).ensure()
        st.add_inbox("alice", "My SSN is 123.", topic="private",
                     extra_frontmatter={"no_promote": True})
        lib = Librarian(st, classifier=_fixed(conflict.NOVEL))
        report = asyncio.run(lib.run())
        assert report.skipped_no_promote == 1
        assert st.read_domain_page("private") is None  # never promoted
        assert len(st.list_inbox("alice")) == 1  # stays in the user's inbox
        print("OK pipeline_no_promote_is_skipped")


def test_pipeline_idempotent_on_quarantine():
    with tempfile.TemporaryDirectory() as root:
        st = WikiStore.for_tenant("acme", root=root).ensure()
        st.set_setting("corroboration_n", 5)  # force CONTEST→queue, never overturn
        st.write_domain_page(Page("fact", {}, "The sky is green.\n"))
        st.add_inbox("alice", "The sky is blue.", topic="fact", sources=["o"])
        lib = Librarian(st, classifier=_fixed(conflict.CONTRADICTION))
        asyncio.run(lib.run())
        q1 = len(st.list_quarantine())
        # second run: the claim is archived (CONTEST published) — nothing new
        # queued; the queue does not grow.
        asyncio.run(Librarian(st, classifier=_fixed(conflict.CONTRADICTION)).run())
        assert len(st.list_quarantine()) == q1, "re-run must not duplicate review notes"
        print("OK pipeline_idempotent_on_quarantine")


def test_pipeline_human_override_promotes_held_claim():
    with tempfile.TemporaryDirectory() as root:
        st = WikiStore.for_tenant("acme", root=root).ensure()
        st.set_setting("corroboration_n", 2)
        st.write_domain_page(Page("fact", {}, "The sky is green.\n"))
        # two UNCITED corroborators → support 2≥N but uncited → QUARANTINE, held
        st.add_inbox("alice", "The sky is blue.", topic="fact")
        st.add_inbox("bob", "The sky is blue.", topic="fact")
        lib = Librarian(st, classifier=_fixed(conflict.CONTRADICTION))
        asyncio.run(lib.run())
        held = st.list_inbox("alice")
        assert len(held) == 1, held
        ch = conflict.claim_hash("fact", held[0].page.body.strip())
        # admin accepts it in the review queue
        st.set_sticky(ch, action="accept", by="human", note="verified")
        asyncio.run(Librarian(st, classifier=_fixed(conflict.CONTRADICTION)).run())
        assert "blue" in st.read_domain_page("fact").body, "human accept should publish"
        assert st.list_inbox("alice") == []  # now archived
        print("OK pipeline_human_override_promotes_held_claim")


def main():
    test_resolve_auto_tier()
    test_resolve_contradiction_threshold()
    test_resolve_human_override_wins()
    test_claim_hash_stable_and_normalized()
    test_pipeline_novel_add_and_archive()
    test_pipeline_supersede_appends_validity()
    test_pipeline_corroborated_overturn_vs_contest()
    test_pipeline_uncited_overturn_quarantines_and_holds_inbox()
    test_pipeline_no_promote_is_skipped()
    test_pipeline_idempotent_on_quarantine()
    test_pipeline_human_override_promotes_held_claim()
    print("\nall wiki-conflict tests passed")


if __name__ == "__main__":
    main()
