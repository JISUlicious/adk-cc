"""Storage abstraction for markdown documents + search.

Both the wiki (shared domain knowledge) and the memory system (per-user
autonomous facts) persist the same shape of thing: a markdown document with
YAML frontmatter, organized into named collections, that must be SEARCHABLE.

The migration requirement drives the design: today everything lives on the
filesystem, but it must be movable to a storage service (SQLite FTS, an
object store + search service, a vector DB) WITHOUT the callers changing and
WITHOUT losing search. The trap is modeling storage as blob get/put — then
search becomes the caller's problem and breaks on migration. So `search` is a
FIRST-CLASS method of the contract: each backend implements its best search
(the filesystem backend does lexical scoring; a service backend can do native
FTS / vector), and callers (wiki query, memory recall) never see how.

Mirrors the codebase's existing storage-abstraction pattern (the artifact
service: `file://` vs `s3://` behind one interface, selected by URI).

Two planes:
  - DOCUMENTS — searchable markdown (`put_doc`/`get_doc`/…/`search`).
  - CONTROL-PLANE KV — small state that is NOT search material (settings,
    sticky resolutions, quarantine notes, an append-only changelog, the
    index/schema singletons). Kept on the same backend so the whole store
    migrates together, but separate from the searchable document plane.

A `collection` is a logical namespace (e.g. `domain/wiki`, `users/alice/inbox`,
`users/alice/episodic`). Tenant scoping is handled by the store factory (a
path prefix for filesystem, a namespace for a service), not here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Sequence


@dataclass
class Document:
    """A stored markdown document: an id, YAML frontmatter, a body."""

    doc_id: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""


@dataclass
class Hit:
    """A search result. `collection` lets the caller tag scope (e.g. shared
    wiki vs. the user's inbox). `frontmatter` is returned so callers can read
    title/contested/etc. without a second fetch."""

    collection: str
    doc_id: str
    score: float
    snippet: str
    frontmatter: dict[str, Any] = field(default_factory=dict)


class DocumentStore(ABC):
    """Backend-agnostic store for searchable markdown documents + small
    control-plane state. Filesystem is the default impl; a service backend
    swaps in behind this interface (selected by URI in the factory)."""

    # ----- searchable documents -----
    @abstractmethod
    def put_doc(self, collection: str, doc: Document) -> None:
        """Create or replace a document (atomic from a reader's view)."""

    @abstractmethod
    def get_doc(self, collection: str, doc_id: str) -> Optional[Document]:
        ...

    @abstractmethod
    def delete_doc(self, collection: str, doc_id: str) -> bool:
        """Returns True if a document was removed, False if absent."""

    @abstractmethod
    def list_ids(self, collection: str) -> list[str]:
        ...

    @abstractmethod
    def list_collections(self, prefix: str) -> list[str]:
        """Immediate child collection names under `prefix` (e.g. the user ids
        under `users`). Lets callers enumerate per-user collections without a
        separate registry."""

    @abstractmethod
    def iter_docs(self, collection: str) -> Iterator[Document]:
        ...

    @abstractmethod
    def move_doc(self, src_collection: str, doc_id: str, dst_collection: str) -> bool:
        """Move a document between collections (e.g. inbox → merged). Returns
        False if the source was absent."""

    @abstractmethod
    def search(
        self, collections: Sequence[str], query: str, *, limit: int = 10
    ) -> list[Hit]:
        """Rank documents across `collections` for `query`, best first. THE
        capability that must survive a backend migration — each backend
        implements its strongest search."""

    # ----- control-plane KV (small, non-searchable state) -----
    @abstractmethod
    def kv_get(self, key: str) -> Optional[str]:
        ...

    @abstractmethod
    def kv_put(self, key: str, value: str) -> None:
        ...

    @abstractmethod
    def kv_delete(self, key: str) -> bool:
        ...

    @abstractmethod
    def kv_list(self, prefix: str) -> list[str]:
        """Keys under `prefix` (e.g. all `resolutions/…`)."""

    @abstractmethod
    def append(self, key: str, line: str) -> None:
        """Append a line to an append-only log (e.g. the changelog)."""
