"""LLM-wiki: shared domain-knowledge base for adk-cc.

A tenant-scoped, compilation-not-retrieval wiki (Karpathy's llm-wiki model).
EXPLICIT by design — users ingest documents and query; the wiki does not
auto-capture. A two-scope flow: users write captures into their PRIVATE
inbox; an offline librarian periodically merges inboxes into the SHARED
domain wiki, classifying and resolving conflicts (single-writer, so textual
merges never race). Markdown + frontmatter + wikilinks.

Storage is backend-agnostic via `adk_cc.docstore.DocumentStore` (filesystem
today; SQLite-FTS / object-store / vector backends swap in by URI without
losing search). This package is the WIKI; per-user autonomous memory lives
in the sibling `adk_cc.memory` package.

Layers:
  - `page`      — Page model: frontmatter + body + [[wikilinks]].
  - `store`     — WikiStore facade over a DocumentStore (collections + KV).
  - `search`    — scope-tagged search + budgeted recall + read-time discrepancy.
  - `conflict`  — classify inbox-vs-domain claims + resolve (pure policy).
  - `librarian` — the offline merge/lint agent + pipeline.

Surfaced as: `wiki_search`/`wiki_read`/`wiki_add` tools (user-scope writes
only), a recall plugin, and a cron-run librarian. Gated by `ADK_CC_WIKI=1`.
"""

from __future__ import annotations

from .librarian import Librarian, LlmClassifier, MergeReport
from .page import Page, parse, serialize, slugify
from .store import InboxDoc, WikiStore, wiki_root_from_env

__all__ = [
    "Page",
    "parse",
    "serialize",
    "slugify",
    "InboxDoc",
    "WikiStore",
    "wiki_root_from_env",
    "Librarian",
    "LlmClassifier",
    "MergeReport",
]
