"""Lexical search + budgeted recall over a tenant's wiki.

At the hundreds-of-pages scale Karpathy targets, embeddings are overkill:
a token-overlap score over title + body, with the index page as the
navigational hand, is enough and stays debuggable. Two entry points:

  - `search(...)` — rank pages for a query. A user's own inbox captures
    are searched alongside domain pages and tagged with their scope so the
    caller can see "this came from your private notes, not the shared wiki".
  - `recall_context(...)` — assemble a SMALL (token-budgeted) context block
    for injection into the model: the index, then top pages, then any
    read-time discrepancy between the user's inbox and the domain. This is
    the Hermes "tiny always-injected memory" surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .page import Page
from .store import WikiStore

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# ~4 chars/token is the usual rough rule; we budget conservatively.
_CHARS_PER_TOKEN = 4


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _score(query_tokens: set[str], page: Page) -> float:
    """Overlap score: title matches weigh more than body matches, and we
    reward distinct query-term coverage over raw term frequency so a page
    that mentions every query word beats one that repeats a single word."""
    if not query_tokens:
        return 0.0
    title_toks = set(_tokens(page.title))
    body_toks = _tokens(page.body)
    body_set = set(body_toks)
    body_freq = {}
    for t in body_toks:
        body_freq[t] = body_freq.get(t, 0) + 1
    covered = query_tokens & (title_toks | body_set)
    if not covered:
        return 0.0
    coverage = len(covered) / len(query_tokens)
    title_hits = len(query_tokens & title_toks)
    body_hits = sum(min(body_freq.get(t, 0), 3) for t in query_tokens)
    return coverage * 10 + title_hits * 3 + body_hits


@dataclass
class Hit:
    slug: str
    title: str
    scope: str  # "domain" | "inbox"
    score: float
    snippet: str
    contested: bool = False


def _snippet(page: Page, query_tokens: set[str], width: int = 220) -> str:
    """A short excerpt around the first query-term hit, else the head."""
    body = page.body.strip()
    low = body.lower()
    pos = -1
    for t in query_tokens:
        i = low.find(t)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        return body[:width].replace("\n", " ").strip()
    start = max(0, pos - width // 3)
    return body[start: start + width].replace("\n", " ").strip()


def search(
    store: WikiStore,
    query: str,
    *,
    user_id: Optional[str] = None,
    limit: int = 5,
) -> list[Hit]:
    """Rank domain pages (and the caller's inbox, if `user_id` given) for a
    query. Inbox hits are tagged scope='inbox'. Sorted by score desc."""
    q = set(_tokens(query))
    hits: list[Hit] = []

    for slug in store.list_domain_pages():
        page = store.read_domain_page(slug)
        if page is None:
            continue
        s = _score(q, page)
        if s > 0:
            hits.append(
                Hit(slug, page.title, "domain", s, _snippet(page, q), page.contested)
            )

    if user_id:
        for doc in store.list_inbox(user_id):
            s = _score(q, doc.page)
            if s > 0:
                hits.append(
                    Hit(doc.slug, doc.page.title, "inbox", s, _snippet(doc.page, q))
                )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


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
