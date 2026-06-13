"""LLM-wiki memory system for adk-cc.

A tenant-scoped, compilation-not-retrieval wiki (Karpathy's llm-wiki model)
with a two-scope flow: users write captures into their PRIVATE inbox; an
offline librarian periodically merges inboxes into the SHARED domain wiki,
classifying and resolving conflicts (single-writer, so textual merges never
race). Markdown + frontmatter + wikilinks on the filesystem; lexical search,
no embeddings.

Layers:
  - `page`      — Page model: frontmatter + body + [[wikilinks]].
  - `store`     — tenant-scoped filesystem layout + atomic IO + lifecycle.
  - `search`    — lexical search + budgeted recall + read-time discrepancy.
  - `conflict`  — (Phase 3) classify inbox-vs-domain claims + resolve.
  - `librarian` — (Phase 3) the offline merge/lint agent + pipeline.

Surfaced to the agent as: `wiki_search`/`wiki_read`/`wiki_add` tools
(user-scope writes only), a `WikiRecallPlugin` (budgeted injection +
auto-capture), and a cron-run librarian. All gated by `ADK_CC_WIKI=1`.
"""

from __future__ import annotations

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
]
