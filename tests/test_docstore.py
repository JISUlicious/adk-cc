"""Tests for the DocumentStore abstraction (docstore/).

Covers the FilesystemDocumentStore contract directly: document CRUD, the
search capability (the part that must survive a backend migration),
collection listing, inbox→merged moves, the control-plane KV + append, and
path-traversal safety. Plus the factory's URI selection. Hand-rolled.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.docstore import Document, FilesystemDocumentStore, make_document_store


def _store(root: str) -> FilesystemDocumentStore:
    return FilesystemDocumentStore(root)


def test_doc_crud_roundtrip():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.put_doc("domain/wiki", Document("openai", {"title": "OpenAI"}, "An AI lab.\n"))
        got = st.get_doc("domain/wiki", "openai")
        assert got is not None and got.frontmatter["title"] == "OpenAI"
        assert got.body.strip() == "An AI lab."
        assert st.list_ids("domain/wiki") == ["openai"]
        assert st.get_doc("domain/wiki", "missing") is None
        assert st.delete_doc("domain/wiki", "openai") is True
        assert st.delete_doc("domain/wiki", "openai") is False
        assert st.list_ids("domain/wiki") == []
    print("OK doc_crud_roundtrip")


def test_search_is_part_of_the_contract():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.put_doc("domain/wiki", Document("openai", {"title": "OpenAI"}, "OpenAI builds GPT.\n"))
        st.put_doc("domain/wiki", Document("anthropic", {"title": "Anthropic"}, "Builds Claude.\n"))
        st.put_doc("users/alice/inbox", Document("n1", {"title": "note"}, "OpenAI pricing changed.\n"))
        hits = st.search(["domain/wiki", "users/alice/inbox"], "OpenAI pricing", limit=5)
        cols = {h.collection for h in hits}
        assert "domain/wiki" in cols and "users/alice/inbox" in cols, cols
        # ranked, frontmatter carried for the caller
        assert hits[0].score >= hits[-1].score
        assert any(h.frontmatter.get("title") for h in hits)
        # no false positives
        assert st.search(["domain/wiki"], "zzzqqq nonsense", limit=5) == []
    print("OK search_is_part_of_the_contract")


def test_collections_and_move():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        st.put_doc("users/alice/inbox", Document("d1", {}, "x"))
        st.put_doc("users/bob/inbox", Document("d2", {}, "y"))
        assert st.list_collections("users") == ["alice", "bob"]
        assert st.list_collections("nope") == []
        # move inbox→merged
        assert st.move_doc("users/alice/inbox", "d1", "users/alice/merged") is True
        assert st.list_ids("users/alice/inbox") == []
        assert st.list_ids("users/alice/merged") == ["d1"]
        assert st.move_doc("users/alice/inbox", "d1", "users/alice/merged") is False
    print("OK collections_and_move")


def test_kv_and_append():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        assert st.kv_get("settings") is None
        st.kv_put("settings", '{"n": 2}')
        assert st.kv_get("settings") == '{"n": 2}'
        st.kv_put("quarantine/abc", "1")
        st.kv_put("quarantine/def", "2")
        assert sorted(st.kv_list("quarantine")) == ["abc", "def"]
        assert st.kv_delete("quarantine/abc") is True
        assert st.kv_list("quarantine") == ["def"]
        st.append("changelog", "line1")
        st.append("changelog", "line2")
        assert st.kv_get("changelog") == "line1\nline2\n"
        # kv must NOT leak into document search
        st.put_doc("c", Document("doc", {}, "real document"))
        hits = st.search(["c"], "document", limit=5)
        assert [h.doc_id for h in hits] == ["doc"]
    print("OK kv_and_append")


def test_path_traversal_guarded():
    with tempfile.TemporaryDirectory() as root:
        st = _store(root)
        for bad in ("../etc", "a/../b"):
            try:
                st.get_doc("domain/wiki", bad)
                raise AssertionError(f"expected reject for doc_id {bad!r}")
            except ValueError:
                pass
        try:
            st.put_doc("../escape", Document("d", {}, "x"))
            raise AssertionError("expected reject for collection")
        except ValueError:
            pass
    print("OK path_traversal_guarded")


def test_factory_uri_selection():
    with tempfile.TemporaryDirectory() as root:
        # default / file:// → filesystem, tenant is a path prefix
        s1 = make_document_store(uri=None, tenant_id="acme", default_root=root)
        assert isinstance(s1, FilesystemDocumentStore)
        assert s1.base == os.path.join(root, "acme")
        s2 = make_document_store(uri=f"file://{root}", tenant_id="beta", default_root="/unused")
        assert s2.base == os.path.join(root, "beta")
        # unimplemented scheme fails loudly (so search can't be silently lost)
        try:
            make_document_store(uri="s3://bucket/x", tenant_id="acme", default_root=root)
            raise AssertionError("expected NotImplementedError for s3://")
        except NotImplementedError:
            pass
        # bad tenant rejected
        try:
            make_document_store(uri=None, tenant_id="../x", default_root=root)
            raise AssertionError("expected reject for bad tenant")
        except ValueError:
            pass
    print("OK factory_uri_selection")


def main():
    test_doc_crud_roundtrip()
    test_search_is_part_of_the_contract()
    test_collections_and_move()
    test_kv_and_append()
    test_path_traversal_guarded()
    test_factory_uri_selection()
    print("\nall docstore tests passed")


if __name__ == "__main__":
    main()
