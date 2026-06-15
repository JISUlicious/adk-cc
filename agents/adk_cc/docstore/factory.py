"""Select a DocumentStore backend from a URI — mirrors the artifact service's
`ADK_CC_ARTIFACT_STORAGE_URI` (`file://` vs `s3://`) pattern.

Today only `file://` is implemented. Future backends (`sqlite://` for FTS,
`s3://`+search, a vector DB) slot in here and callers never change — that's
the whole point of the abstraction. The tenant becomes a path prefix for the
filesystem backend and a namespace for a service backend.
"""

from __future__ import annotations

import os
from typing import Optional

from .base import DocumentStore
from .filesystem import FilesystemDocumentStore, _safe_segment


def make_document_store(
    *, uri: Optional[str], tenant_id: str, default_root: str
) -> DocumentStore:
    """Build the per-tenant document store.

    `uri` (e.g. from ADK_CC_WIKI_STORE_URI / ADK_CC_MEMORY_STORE_URI): when
    unset or `file://<root>`, a FilesystemDocumentStore rooted at
    `<root>/<tenant>`. Service schemes raise NotImplementedError until a
    backend is added — loud-by-design so an operator can't silently lose
    search by pointing at an unimplemented store.
    """
    tenant = _safe_segment(tenant_id or "local", "tenant_id")
    if not uri or uri.startswith("file://"):
        root = uri[len("file://"):] if uri else default_root
        root = os.path.abspath(os.path.expanduser(root or default_root))
        return FilesystemDocumentStore(os.path.join(root, tenant))
    scheme = uri.split("://", 1)[0]
    raise NotImplementedError(
        f"document store scheme {scheme!r} is not implemented yet "
        f"(available: file://). Add a backend in adk_cc/docstore/ — it must "
        f"implement DocumentStore.search so search survives the migration."
    )
