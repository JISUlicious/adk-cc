"""llm-wiki skill frontmatter conformance: type / tags / updated. Model-free."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.wiki import Librarian, WikiStore
from adk_cc.wiki.page import Page, normalize_tags


def _store():
    return WikiStore.for_tenant("acme", root=tempfile.mkdtemp(prefix="wskill-")).ensure()


# ---------- normalize_tags (skill tag policy) ----------
def test_normalize_tags_kebab_dedup_cap3():
    assert normalize_tags(["CPU", "ISCA 2024", "cpu", "Deep Learning", "x"]) == \
        ["cpu", "isca-2024", "deep-learning"]  # kebab, deduped, max 3
    assert normalize_tags("Single Tag") == ["single-tag"]
    assert normalize_tags(None) == []


# ---------- Page accessors + defaults ----------
def test_page_type_defaults_and_validates():
    assert Page("s", {}, "").type == "concept"            # default
    assert Page("s", {"type": "entity"}, "").type == "entity"
    assert Page("s", {"type": "bogus"}, "").type == "concept"  # invalid → default
    assert Page("s", {"tags": ["a", "b"]}, "").tags == ["a", "b"]
    assert Page("s", {"created": "T1"}, "").updated == "T1"     # falls back to created
    assert Page("s", {"created": "T1", "updated": "T2"}, "").updated == "T2"


# ---------- inbox capture stamps the skill fields ----------
def test_add_inbox_sets_type_tags_timestamps():
    w = _store()
    d = w.add_inbox("alice", "TAGE predicts branches.", topic="branch-prediction",
                    type="concept", tags=["CPU", "cpu", "ISCA 2024"])
    fm = d.page.frontmatter
    assert fm["type"] == "concept"
    assert fm["tags"] == ["cpu", "isca-2024"]      # normalized + deduped
    assert fm["created"] and fm["updated"]          # both stamped
    # invalid type → concept
    d2 = w.add_inbox("alice", "x", topic="t", type="nonsense")
    assert d2.page.frontmatter["type"] == "concept"


# ---------- librarian carries type/tags + stamps updated on the domain page ----------
def test_librarian_propagates_skill_frontmatter():
    w = _store()
    w.add_inbox("alice", "Out-of-order execution uses a reorder buffer.",
                topic="out-of-order-execution", type="concept", tags=["microarch"])
    asyncio.run(Librarian(w).run())
    p = w.read_domain_page("out-of-order-execution")
    assert p is not None
    assert p.type == "concept"
    assert p.tags == ["microarch"]
    assert p.frontmatter.get("updated") and p.frontmatter.get("created")


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
    print("\nall wiki-skill-frontmatter tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
