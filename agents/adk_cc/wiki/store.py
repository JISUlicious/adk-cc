"""Tenant-scoped wiki store — a thin facade over a `DocumentStore`.

The wiki is the SHARED domain-knowledge base (the librarian is its only
writer). This module maps the wiki's semantics onto the backend-agnostic
`docstore.DocumentStore` so the whole thing can migrate from the filesystem
to a storage service (SQLite FTS, object store + search, vector DB) without
the librarian / tools / conflict logic changing — and without losing search,
which is a first-class method of the store contract.

Logical layout (collections + control-plane KV, not filesystem paths):
    documents (searchable):
      domain/wiki              shared pages (doc_id = slug)
      users/<uid>/inbox        user-scope captures awaiting merge
      users/<uid>/merged       archived post-merge (the user keeps a copy)
    control-plane KV (not searched):
      schema, index            the conventions doc + the nav index
      settings                 per-tenant admin knobs (corroboration_n)
      sources/<id>             immutable ingested originals (provenance)
      resolutions/<hash>       sticky resolutions (idempotency + adjudication)
      quarantine/<hash>        human review queue
      changelog                append-only merge log

Backend selected by `ADK_CC_WIKI_STORE_URI` (default `file://<root>`); the
tenant is a path prefix (filesystem) or namespace (service).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..docstore import Document, DocumentStore, make_document_store
from . import page as pagelib
from .page import Page

# --- defaults the admin panel can override per-tenant (settings) ---
_DEFAULT_CORROBORATION_N = 2

_DOMAIN = "domain/wiki"


def _safe_id(value: str, label: str) -> str:
    """Reject path-traversal in the tenant id before it reaches the store."""
    safe = "".join(c for c in value if c.isalnum() or c in "-_")
    if safe != value or not safe:
        raise ValueError(f"unsafe {label}: {value!r}")
    return safe


def wiki_root_from_env() -> str:
    """Default filesystem root when no store URI is configured. Explicit
    `ADK_CC_WIKI_ROOT` wins; otherwise a `.wiki` sibling of the workspace
    root (or CWD in dev)."""
    raw = os.environ.get("ADK_CC_WIKI_ROOT")
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    base = os.environ.get("ADK_CC_WORKSPACE_ROOT") or os.getcwd()
    return os.path.join(os.path.abspath(os.path.expanduser(base)), ".wiki")


def corroboration_default_from_env() -> int:
    try:
        return max(1, int(os.environ.get("ADK_CC_WIKI_CORROBORATION_N", "")))
    except ValueError:
        return _DEFAULT_CORROBORATION_N


_DEFAULT_SCHEMA = """\
# Wiki schema & conventions

This wiki is COMPILED, not retrieved: each page is a living summary of one
entity or concept, maintained by the librarian — not an append-only log.

## Page shape
- One markdown file per entity/concept; filename is the slug.
- Optional YAML frontmatter: `title`, `sources` (doc ids backing claims),
  `no_promote`/`sensitive` (never merged to domain), `contested` (records a
  true contradiction), `validity` (supersession windows), `captured_by`,
  `created`.
- Body is prose with `[[wikilink]]` cross-references. `index.md` is the
  hand of the wiki: a short map of the top-level pages.

## Claims
- Prefer specific, dated, sourced statements over vague ones.
- Every promoted claim cites a source (cite-or-quarantine).
- When two sources disagree and neither supersedes the other, record BOTH
  and mark the page `contested` — do not silently pick a winner.
"""


@dataclass
class InboxDoc:
    """A user-scope capture awaiting merge. `doc_id` is the unique id; `slug`
    is the topic it concerns (may collide across docs)."""

    doc_id: str
    slug: str
    page: Page


def _inbox(user_id: str) -> str:
    return f"users/{_safe_id(user_id, 'user_id')}/inbox"


def _merged(user_id: str) -> str:
    return f"users/{_safe_id(user_id, 'user_id')}/merged"


class WikiStore:
    """Facade mapping wiki semantics onto a `DocumentStore`. Construct via
    `for_tenant`; call `ensure()` once before first use."""

    DOMAIN_COLLECTION = _DOMAIN

    @staticmethod
    def inbox_collection(user_id: str) -> str:
        return _inbox(user_id)

    def __init__(self, tenant_id: str, store: DocumentStore) -> None:
        self.tenant_id = tenant_id
        self._store = store

    @classmethod
    def for_tenant(cls, tenant_id: str, root: Optional[str] = None) -> "WikiStore":
        tid = _safe_id(tenant_id or "local", "tenant_id")
        store = make_document_store(
            uri=os.environ.get("ADK_CC_WIKI_STORE_URI"),
            tenant_id=tid,
            default_root=root or wiki_root_from_env(),
        )
        return cls(tenant_id=tid, store=store)

    @property
    def store(self) -> DocumentStore:
        return self._store

    # ----- bring-up -----
    def ensure(self) -> "WikiStore":
        """Seed the conventions doc + empty index (idempotent)."""
        if self._store.kv_get("schema") is None:
            self._store.kv_put("schema", _DEFAULT_SCHEMA)
        if self._store.kv_get("index") is None:
            self._store.kv_put("index", "# Index\n\n_(empty — no pages yet)_\n")
        return self

    # ----- domain pages (read by everyone; written only by the librarian) -----
    def list_domain_pages(self) -> list[str]:
        return self._store.list_ids(_DOMAIN)

    def read_domain_page(self, slug: str) -> Optional[Page]:
        doc = self._store.get_doc(_DOMAIN, slug)
        return Page(slug=slug, frontmatter=doc.frontmatter, body=doc.body) if doc else None

    def write_domain_page(self, page: Page) -> None:
        self._store.put_doc(
            _DOMAIN, Document(page.slug, dict(page.frontmatter), page.body)
        )

    def delete_domain_page(self, slug: str) -> Optional[Page]:
        """Remove a domain page (used by compaction). Returns the removed page
        (so the caller can log it for rollback) or None if it didn't exist."""
        page = self.read_domain_page(slug)
        if page is None:
            return None
        self._store.delete_doc(_DOMAIN, slug)
        return page

    def read_index(self) -> str:
        return self._store.kv_get("index") or ""

    def write_index(self, text: str) -> None:
        self._store.kv_put("index", text)

    def read_schema(self) -> str:
        return self._store.kv_get("schema") or ""

    # ----- user inbox (user-scope captures) -----
    def add_inbox(
        self,
        user_id: str,
        text: str,
        *,
        title: Optional[str] = None,
        topic: Optional[str] = None,
        sources: Optional[list[str]] = None,
        type: Optional[str] = None,
        tags: Optional[list[str]] = None,
        extra_frontmatter: Optional[dict[str, Any]] = None,
        doc_id: Optional[str] = None,
    ) -> InboxDoc:
        """Capture a doc/claim into the user's inbox. Slug from `topic` else
        `title` else the first body line; `doc_id` unique (`<slug>__<hash8>`)
        unless given. Idempotent when an explicit `doc_id` is reused. `type`
        (entity|concept|source|comparison|query, default concept) and `tags`
        (≤3 kebab) follow the llm-wiki skill schema."""
        slug = pagelib.slugify(topic or title or _first_line(text)) or "note"
        if doc_id is None:
            doc_id = f"{slug}__{_short_hash(text)}"
        doc_id = _safe_id(doc_id, "doc_id")
        now = _now_iso()
        fm: dict[str, Any] = {
            "title": title or _first_line(text) or slug,
            "slug": slug,
            "type": type if type in pagelib._PAGE_TYPES else "concept",
            "captured_by": user_id,
            "created": now,
            "updated": now,
        }
        norm_tags = pagelib.normalize_tags(tags)
        if norm_tags:
            fm["tags"] = norm_tags
        if sources:
            fm["sources"] = list(sources)
        if extra_frontmatter:
            fm.update(extra_frontmatter)
        body = text.strip() + "\n"
        self._store.put_doc(_inbox(user_id), Document(doc_id, fm, body))
        return InboxDoc(doc_id=doc_id, slug=slug, page=Page(slug, fm, body))

    def list_inbox(self, user_id: str) -> list[InboxDoc]:
        out: list[InboxDoc] = []
        for doc in self._store.iter_docs(_inbox(user_id)):
            slug = str(doc.frontmatter.get("slug") or doc.doc_id)
            out.append(
                InboxDoc(doc.doc_id, slug, Page(doc.doc_id, doc.frontmatter, doc.body))
            )
        return out

    def list_user_ids(self) -> list[str]:
        return self._store.list_collections("users")

    def archive_inbox(self, user_id: str, doc_id: str) -> Optional[str]:
        """Move a processed inbox doc → merged (the user keeps the copy).
        Returns the doc_id if moved, None if the source was already gone."""
        did = _safe_id(doc_id, "doc_id")
        return did if self._store.move_doc(_inbox(user_id), did, _merged(user_id)) else None

    def list_merged(self, user_id: str) -> list[str]:
        return self._store.list_ids(_merged(user_id))

    # ----- sources (immutable provenance) -----
    def write_source(self, source_id: str, text: str) -> None:
        key = f"sources/{_safe_id(source_id, 'source_id')}"
        if self._store.kv_get(key) is None:  # immutable: first write wins
            self._store.kv_put(key, text)

    def has_source(self, source_id: str) -> bool:
        return self._store.kv_get(f"sources/{_safe_id(source_id, 'source_id')}") is not None

    def read_source(self, source_id: str) -> Optional[str]:
        return self._store.kv_get(f"sources/{_safe_id(source_id, 'source_id')}")

    # ----- changelog -----
    def append_changelog(self, entry: dict[str, Any]) -> None:
        rec = {"ts": _now_iso(), **entry}
        self._store.append("changelog", json.dumps(rec, ensure_ascii=False))

    def read_changelog(self) -> str:
        return self._store.kv_get("changelog") or ""

    # ----- sticky resolutions (idempotency + human adjudication) -----
    def get_sticky(self, claim_hash: str) -> Optional[dict[str, Any]]:
        raw = self._store.kv_get(f"resolutions/{_safe_id(claim_hash, 'hash')}")
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None

    def set_sticky(
        self, claim_hash: str, *, action: str, by: str = "auto", note: str = ""
    ) -> None:
        self._store.kv_put(
            f"resolutions/{_safe_id(claim_hash, 'hash')}",
            json.dumps({"action": action, "by": by, "note": note, "ts": _now_iso()}),
        )

    def human_override(self, claim_hash: str) -> Optional[str]:
        rec = self.get_sticky(claim_hash)
        if rec and rec.get("by") == "human" and rec.get("action") in ("accept", "reject"):
            return rec["action"]
        return None

    # ----- quarantine (human review queue) -----
    def add_quarantine(self, claim_hash: str, record: dict[str, Any]) -> str:
        h = _safe_id(claim_hash, "hash")
        rec = {"claim_hash": claim_hash, "status": "pending", "ts": _now_iso(), **record}
        self._store.kv_put(f"quarantine/{h}", json.dumps(rec))
        return h

    def list_quarantine(self, *, pending_only: bool = True) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key in self._store.kv_list("quarantine"):
            raw = self._store.kv_get(f"quarantine/{key}")
            if raw is None:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if pending_only and rec.get("status") != "pending":
                continue
            out.append(rec)
        return out

    def is_quarantined(self, claim_hash: str) -> bool:
        return self._store.kv_get(f"quarantine/{_safe_id(claim_hash, 'hash')}") is not None

    # ----- per-tenant settings (admin-tunable) -----
    def read_settings(self) -> dict[str, Any]:
        raw = self._store.kv_get("settings")
        if raw is None:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def write_settings(self, settings: dict[str, Any]) -> None:
        self._store.kv_put("settings", json.dumps(settings, indent=2))

    def set_setting(self, key: str, value: Any) -> dict[str, Any]:
        s = self.read_settings()
        s[key] = value
        self.write_settings(s)
        return s

    @property
    def corroboration_n(self) -> int:
        """How many independent users must corroborate a claim to overturn a
        domain fact without human adjudication. Admin settings wins; else the
        env default (`ADK_CC_WIKI_CORROBORATION_N`, then 2)."""
        raw = self.read_settings().get("corroboration_n")
        try:
            if raw is not None:
                return max(1, int(raw))
        except (TypeError, ValueError):
            pass
        return corroboration_default_from_env()


# --------------------------------------------------------------------------
# small free helpers
# --------------------------------------------------------------------------
def _first_line(text: str) -> str:
    for line in text.splitlines():
        s = line.strip().lstrip("# ").strip()
        if s:
            return s[:120]
    return ""


def _short_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
