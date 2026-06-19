"""Backend-agnostic document storage + search.

The base layer under both the wiki (shared domain) and the memory system
(per-user). Markdown documents in named collections, with SEARCH as a
first-class method of the contract so the store can migrate from the
filesystem to a service (SQLite FTS, object store + search, vector DB)
without callers changing or losing search. See `base.DocumentStore`.
"""

from __future__ import annotations

from .base import Document, DocumentStore, Hit
from .factory import make_document_store
from .filesystem import FilesystemDocumentStore

__all__ = [
    "Document",
    "DocumentStore",
    "Hit",
    "FilesystemDocumentStore",
    "make_document_store",
]
