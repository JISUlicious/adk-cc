"""Tests for the wiki memory store/page/search layers (Phase 1).

Covers the filesystem store (layout, atomic IO, inbox→merged lifecycle,
per-tenant settings), the Page parser/serializer (frontmatter + wikilinks),
and lexical search (domain+inbox overlay, read-time discrepancy, budgeted
recall). Hand-rolled (no pytest); uses a tmp dir as ADK_CC_WIKI_ROOT.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.wiki import page as pagelib
from adk_cc.wiki import search as searchlib
from adk_cc.wiki.page import Page
from adk_cc.wiki.store import WikiStore, corroboration_default_from_env


def _store(root: str, tenant: str = "acme") -> WikiStore:
    return WikiStore.for_tenant(tenant, root=root).ensure()


# ---------------- page.py ----------------
def test_page_roundtrip():
    text = (
        "---\n"
        "title: GPT-4 Turbo\n"
        "sources: [doc-a, doc-b]\n"
        "no_promote: true\n"
        "---\n\n"
        "# GPT-4 Turbo\n\n"
        "A model by [[OpenAI|OpenAI Inc]]. Compare [[gpt-4]].\n"
    )
    p = pagelib.parse(text, "gpt-4-turbo")
    assert p.title == "GPT-4 Turbo"
    assert p.sources == ["doc-a", "doc-b"]
    assert p.no_promote is True
    assert p.wikilinks == ["openai", "gpt-4"], p.wikilinks
    # serialize → parse is stable
    p2 = pagelib.parse(pagelib.serialize(p), "gpt-4-turbo")
    assert p2.frontmatter == p.frontmatter
    assert p2.body.strip() == p.body.strip()
    print("OK page_roundtrip")


def test_page_no_frontmatter_and_malformed():
    plain = pagelib.parse("# Hello\n\nbody only\n", "hello")
    assert plain.frontmatter == {}
    assert plain.title == "Hello"
    # malformed frontmatter is tolerated (treated as no fm, never raises)
    bad = pagelib.parse("---\n: : :bad yaml\n---\nbody\n", "x")
    assert isinstance(bad.frontmatter, dict)
    assert "body" in bad.body
    # sensitive aliases no_promote
    assert pagelib.parse("---\nsensitive: true\n---\nx\n", "s").no_promote is True
    print("OK page_no_frontmatter_and_malformed")


def test_slugify():
    assert pagelib.slugify("GPT-4 Turbo!") == "gpt-4-turbo"
    assert pagelib.slugify("  Multiple   Spaces  ") == "multiple-spaces"
    assert pagelib.slugify("///") == ""
    print("OK slugify")


# ---------------- store.py ----------------
def test_store_skeleton_and_domain_page():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        # ensure() seeds the conventions doc + an empty index (via the API,
        # not raw paths — the store is backend-agnostic now).
        assert "schema" in st.read_schema().lower()
        assert "no pages yet" in st.read_index()
        # write + read a domain page
        st.write_domain_page(Page("openai", {"title": "OpenAI"}, "An AI lab.\n"))
        assert st.list_domain_pages() == ["openai"]
        got = st.read_domain_page("openai")
        assert got is not None and got.title == "OpenAI"
        assert st.read_domain_page("nope") is None
        # index.md is excluded from the page listing
        st.write_index("# Index\n- [[openai]]\n")
        assert "openai" in st.read_index()
        assert st.list_domain_pages() == ["openai"]
    print("OK store_skeleton_and_domain_page")


def test_inbox_capture_and_archive():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        doc = st.add_inbox(
            "alice", "GPT-4 Turbo has a 128k context window.", topic="GPT-4 Turbo"
        )
        assert doc.slug == "gpt-4-turbo"
        listed = st.list_inbox("alice")
        assert len(listed) == 1 and listed[0].doc_id == doc.doc_id
        assert st.list_user_ids() == ["alice"]
        # archive moves inbox→merged (user keeps a copy), inbox now empty
        assert st.archive_inbox("alice", doc.doc_id) == doc.doc_id
        assert st.list_inbox("alice") == []
        assert doc.doc_id in st.list_merged("alice")
        # archiving a gone doc is a safe no-op
        assert st.archive_inbox("alice", doc.doc_id) is None
    print("OK inbox_capture_and_archive")


def test_idempotent_doc_id():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.add_inbox("bob", "fact", topic="t", doc_id="fixed")
        st.add_inbox("bob", "fact updated", topic="t", doc_id="fixed")
        listed = st.list_inbox("bob")
        assert len(listed) == 1, "explicit doc_id reuse must overwrite, not dup"
        assert "updated" in listed[0].page.body
    print("OK idempotent_doc_id")


def test_safe_id_rejects_traversal():
    with tempfile.TemporaryDirectory() as root:
        for bad in ("../etc", "a/b", ".."):
            try:
                WikiStore.for_tenant(bad, root=root)
                raise AssertionError(f"expected reject for {bad!r}")
            except ValueError:
                pass
        # empty tenant id is intentionally defaulted to "local", not rejected
        assert WikiStore.for_tenant("", root=root).tenant_id == "local"
        # but a bad slug/user/doc id IS rejected downstream
        st = _store(root)
        for bad in ("../x", "a/b"):
            try:
                st.read_domain_page(bad)
                raise AssertionError(f"expected slug reject for {bad!r}")
            except ValueError:
                pass
    print("OK safe_id_rejects_traversal")


def test_sources_immutable_and_changelog():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.write_source("src1", "original")
        st.write_source("src1", "OVERWRITE ATTEMPT")
        assert st.read_source("src1") == "original", "sources must be immutable"
        assert st.has_source("src1") and not st.has_source("src2")
        st.append_changelog({"op": "merge", "slug": "openai"})
        log = st.read_changelog()
        assert '"op": "merge"' in log and '"ts"' in log
    print("OK sources_immutable_and_changelog")


def test_settings_corroboration_n():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        # default falls back to env helper (2 absent override)
        assert st.corroboration_n == corroboration_default_from_env()
        st.set_setting("corroboration_n", 3)
        assert WikiStore.for_tenant("acme", root=root).corroboration_n == 3
        # garbage value falls back, never raises
        st.set_setting("corroboration_n", "bogus")
        assert st.corroboration_n == corroboration_default_from_env()
    print("OK settings_corroboration_n")


# ---------------- search.py ----------------
def test_search_overlay_and_scope():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.write_domain_page(
            Page("openai", {"title": "OpenAI"}, "OpenAI builds GPT models.\n")
        )
        st.write_domain_page(
            Page("anthropic", {"title": "Anthropic"}, "Anthropic builds Claude.\n")
        )
        st.add_inbox("alice", "OpenAI released a new pricing tier.", topic="OpenAI")
        hits = searchlib.search(st, "OpenAI pricing", user_id="alice", limit=5)
        scopes = {h.scope for h in hits}
        assert "domain" in scopes and "inbox" in scopes, scopes
        # bob (no inbox) only sees domain
        bob_hits = searchlib.search(st, "OpenAI", user_id="bob")
        assert all(h.scope == "domain" for h in bob_hits)
        # unrelated query returns nothing
        assert searchlib.search(st, "zzzqqq nonsense", user_id="alice") == []
    print("OK search_overlay_and_scope")


def test_find_discrepancies():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.write_domain_page(Page("gpt-4", {}, "Context window is 8k tokens.\n"))
        st.add_inbox("alice", "Context window is now 128k tokens.", topic="gpt-4")
        st.add_inbox("alice", "A wholly new topic.", topic="brand-new")
        discs = searchlib.find_discrepancies(st, "alice")
        assert len(discs) == 1 and discs[0].slug == "gpt-4", discs
        assert "8k" in discs[0].domain_excerpt
        assert "128k" in discs[0].inbox_excerpt
    print("OK find_discrepancies")


def test_recall_context_budget():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        for i in range(20):
            st.write_domain_page(
                Page(f"page-{i}", {}, f"Topic gamma number {i} " * 40 + "\n")
            )
        st.write_index("# Index\n" + "\n".join(f"- [[page-{i}]]" for i in range(20)))
        tiny = searchlib.recall_context(st, "gamma", budget_tokens=50)
        big = searchlib.recall_context(st, "gamma", budget_tokens=800)
        assert len(tiny) <= len(big)
        assert len(tiny) <= 50 * 4 + 200, f"budget overrun: {len(tiny)} chars"
        # empty store → empty recall, never raises
        empty = _store(root, tenant="empty")
        assert searchlib.recall_context(empty, "anything") == ""
    print("OK recall_context_budget")


def main():
    test_page_roundtrip()
    test_page_no_frontmatter_and_malformed()
    test_slugify()
    test_store_skeleton_and_domain_page()
    test_inbox_capture_and_archive()
    test_idempotent_doc_id()
    test_safe_id_rejects_traversal()
    test_sources_immutable_and_changelog()
    test_settings_corroboration_n()
    test_search_overlay_and_scope()
    test_find_discrepancies()
    test_recall_context_budget()
    print("\nall wiki-store tests passed")


if __name__ == "__main__":
    main()
