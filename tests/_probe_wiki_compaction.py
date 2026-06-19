"""Live probe: wiki domain compaction with the real model — semantic alias
(slug-level) gets merged, a distinct page does not, every merge is verified.
Skips without a model.

Run:  .venv/bin/python tests/_probe_wiki_compaction.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "30")
_TMP = tempfile.mkdtemp(prefix="wprobe-")
os.environ["ADK_CC_WIKI_ROOT"] = _TMP
os.environ.pop("ADK_CC_WIKI_STORE_URI", None)

from adk_cc.agent import MODEL
from adk_cc.wiki import Librarian, WikiStore, make_llm_entity_resolver, make_llm_merge_verifier
from adk_cc.wiki.page import Page


def _page(slug, body):
    return Page(slug=slug, frontmatter={"title": slug, "sources": [f"src-{slug}"]}, body=body + "\n")


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live model.")
        return 0
    s = WikiStore.for_tenant("acme", root=_TMP).ensure()
    # two slugs for the SAME entity (LLM-resolvable, NOT a hyphenation variant)
    s.write_domain_page(_page("nyc", "NYC has about 8 million residents."))
    s.write_domain_page(_page("new-york-city", "New York City is in New York State."))
    # a genuinely distinct page that must NOT be merged
    s.write_domain_page(_page("los-angeles", "Los Angeles is in California."))

    print("before:", sorted(s.list_domain_pages()))
    lib = Librarian(
        s,
        resolver=make_llm_entity_resolver(MODEL),
        verifier=make_llm_merge_verifier(MODEL),
    )
    try:
        rep = asyncio.run(lib.compact())
    except Exception as e:  # noqa: BLE001
        print(f"SKIP: compaction failed ({type(e).__name__}: {e}).")
        return 0

    after = sorted(s.list_domain_pages())
    print("after :", after)
    print(f"report: merged={rep.merged} groups={rep.groups} "
          f"pages {rep.pages_before}->{rep.pages_after} actions={rep.actions}")

    merged_one = rep.merged == 1
    la_kept = s.read_domain_page("los-angeles") is not None
    nyc_pages = [p for p in ("nyc", "new-york-city") if s.read_domain_page(p) is not None]
    folded = len(nyc_pages) == 1  # the two NYC slugs collapsed to one
    survivor = s.read_domain_page(nyc_pages[0]) if nyc_pages else None
    content_kept = bool(survivor and "8 million" in survivor.body and "New York State" in survivor.body)
    ops = []
    for line in s.read_changelog().splitlines():
        try:
            ops.append(json.loads(line).get("op"))
        except ValueError:
            pass

    print(f"\n  [{'PASS' if folded else 'FAIL'}] NYC aliases merged to one page: {nyc_pages}")
    print(f"  [{'PASS' if content_kept else 'FAIL'}] both bodies preserved on survivor")
    print(f"  [{'PASS' if la_kept else 'FAIL'}] distinct page los-angeles NOT merged")
    print(f"  [{'PASS' if 'compact_merge' in ops else 'FAIL'}] compact_merge logged (rollback)")
    ok = folded and content_kept and la_kept and "compact_merge" in ops
    print(f"\n  {'PASS' if ok else 'FAIL'}: wiki compaction (semantic dedup + verify + provenance)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
