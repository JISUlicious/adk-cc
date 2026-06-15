"""Search + budgeted recall over a tenant's wiki.

Ranking now lives in the `DocumentStore` (so it travels with the storage
backend — a service backend provides native FTS/vector and these functions
don't change). This module keeps the wiki-specific shaping on top: scope
tagging (shared domain vs. the caller's private inbox), read-time discrepancy
surfacing, and the token-budgeted recall block (the Hermes "tiny always-
injected memory" surface).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .store import WikiStore

# ~4 chars/token is the usual rough rule; we budget conservatively.
_CHARS_PER_TOKEN = 4


@dataclass
class Hit:
    slug: str
    title: str
    scope: str  # "domain" | "inbox"
    score: float
    snippet: str
    contested: bool = False


def search(
    store: WikiStore,
    query: str,
    *,
    user_id: Optional[str] = None,
    limit: int = 5,
) -> list[Hit]:
    """Rank domain pages (and the caller's inbox, if `user_id` given) for a
    query, delegating the scoring to the backend. Inbox hits are tagged
    scope='inbox'. Sorted by score desc."""
    collections = [WikiStore.DOMAIN_COLLECTION]
    if user_id:
        collections.append(store.inbox_collection(user_id))
    out: list[Hit] = []
    for h in store.store.search(collections, query, limit=limit):
        domain = h.collection == WikiStore.DOMAIN_COLLECTION
        slug = h.doc_id if domain else str(h.frontmatter.get("slug") or h.doc_id)
        title = str(h.frontmatter.get("title") or slug)
        out.append(
            Hit(
                slug=slug,
                title=title,
                scope="domain" if domain else "inbox",
                score=h.score,
                snippet=h.snippet,
                contested=bool(h.frontmatter.get("contested")),
            )
        )
    return out


@dataclass
class Discrepancy:
    """The caller's inbox holds a claim about a slug that domain also has —
    surfaced at read time so the model can flag the divergence instead of
    silently trusting one scope."""

    slug: str
    domain_excerpt: str
    inbox_excerpt: str


def find_discrepancies(store: WikiStore, user_id: str) -> list[Discrepancy]:
    """Inbox docs whose slug also exists in domain. Read-time conflict
    surfacing: 'your notes differ from the shared wiki on X'. Cheap text
    excerpts only — actual conflict CLASSIFICATION is the librarian's job."""
    out: list[Discrepancy] = []
    domain_slugs = set(store.list_domain_pages())
    seen: set[str] = set()
    for doc in store.list_inbox(user_id):
        if doc.slug in domain_slugs and doc.slug not in seen:
            seen.add(doc.slug)
            dp = store.read_domain_page(doc.slug)
            if dp is None:
                continue
            out.append(
                Discrepancy(
                    slug=doc.slug,
                    domain_excerpt=dp.body.strip()[:200].replace("\n", " "),
                    inbox_excerpt=doc.page.body.strip()[:200].replace("\n", " "),
                )
            )
    return out


def recall_context(
    store: WikiStore,
    query: str,
    *,
    user_id: Optional[str] = None,
    budget_tokens: int = 800,
) -> str:
    """Assemble a budgeted recall block for injection into the model.

    Order (most useful first, truncated when the budget runs out):
      1. the index (the wiki's navigational hand)
      2. top-matching pages for the query, each as title + snippet, tagged
         by scope (shared wiki vs. your private notes)
      3. read-time discrepancies between the caller's notes and domain
    Returns "" when nothing relevant fits — the plugin then injects nothing.
    """
    budget_chars = max(0, budget_tokens) * _CHARS_PER_TOKEN
    parts: list[str] = []
    used = 0

    def _take(block: str) -> bool:
        nonlocal used
        if used + len(block) > budget_chars:
            return False
        parts.append(block)
        used += len(block)
        return True

    index = store.read_index().strip()
    if index and "no pages yet" not in index:
        head = index if len(index) <= budget_chars // 2 else index[: budget_chars // 2]
        _take("## Wiki index\n" + head + "\n")

    hits = search(store, query, user_id=user_id, limit=8)
    if hits:
        lines = ["## Relevant wiki pages"]
        for h in hits:
            tag = "shared" if h.scope == "domain" else "your notes"
            flag = " ⚠️contested" if h.contested else ""
            lines.append(f"- [[{h.slug}]] ({tag}{flag}): {h.snippet}")
        _take("\n".join(lines) + "\n")

    if user_id:
        discs = find_discrepancies(store, user_id)
        if discs:
            lines = [
                "## ⚠️ Your notes differ from the shared wiki",
                "(surface this to the user; do not silently prefer one side)",
            ]
            for d in discs:
                lines.append(
                    f"- **{d.slug}** — shared: {d.domain_excerpt} || "
                    f"your notes: {d.inbox_excerpt}"
                )
            _take("\n".join(lines) + "\n")

    return "\n".join(parts).strip()
