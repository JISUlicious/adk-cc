"""#129-3: the librarian core pointed at ONE user's PERSONAL wiki.

`PersonalWikiView` is a WikiStore that redirects the librarian's writes to
`users/<uid>/wiki/` — the SAME merge engine, conflict policy, sticky
resolutions and quarantine queue run against a personal target
(single-writer principle: never a second synthesis engine).

Scoping rules:
  - "domain"-page ops target `users/<uid>/wiki/` and stamp
    `scope: personal` in the frontmatter (search/graph distinguish them).
  - control-plane KV (resolutions/, quarantine/, changelog, settings,
    schema/index) is namespaced under `users/<uid>/` so a personal
    adjudication can never collide with a domain one for the same claim.
  - the INPUT is the user's inbox PLUS their already-domain-merged docs —
    personal knowledge should not vanish just because the domain librarian
    consumed the note first. Consumption is tracked with a
    `personal-merged/<doc_id>` marker instead of moving the doc, so the
    DOMAIN pass still sees everything (the two passes never race).

Cost note: this runs once per user per librarian invocation — opt-in via
the cron flag / ADK_CC_PERSONAL_WIKI, and reasonable on a lower cadence
than the domain merge.
"""
from __future__ import annotations

from typing import Optional

from .page import Page
from .store import (
    Document,
    InboxDoc,
    WikiStore,
    _inbox,
    _merged,
    _safe_id,
)


class _PrefixedKv:
    """kv_*/append namespaced under a prefix; every other DocumentStore
    method passes through untouched (doc collections keep absolute names)."""

    def __init__(self, inner, prefix: str) -> None:
        self._inner = inner
        self._prefix = prefix

    def kv_get(self, key: str):
        return self._inner.kv_get(self._prefix + key)

    def kv_put(self, key: str, value: str):
        return self._inner.kv_put(self._prefix + key, value)

    def kv_delete(self, key: str):
        return self._inner.kv_delete(self._prefix + key)

    def kv_list(self, prefix: str):
        return self._inner.kv_list(self._prefix + prefix)

    def append(self, key: str, line: str):
        return self._inner.append(self._prefix + key, line)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def personal_pages_collection(user_id: str) -> str:
    return f"users/{_safe_id(user_id, 'user_id')}/wiki"


class PersonalWikiView(WikiStore):
    """A WikiStore whose 'domain' is one user's personal wiki."""

    def __init__(self, base: WikiStore, user_id: str) -> None:
        uid = _safe_id(user_id, "user_id")
        super().__init__(base.tenant_id, _PrefixedKv(base.store, f"users/{uid}/"))
        self.user_id = uid
        self._pages = personal_pages_collection(uid)
        self._raw = base.store  # unprefixed, for doc collections

    # ----- 'domain' ops → personal pages, scope-tagged -----
    def list_domain_pages(self) -> list[str]:
        return self._raw.list_ids(self._pages)

    def read_domain_page(self, slug: str) -> Optional[Page]:
        doc = self._raw.get_doc(self._pages, slug)
        return Page(slug=slug, frontmatter=doc.frontmatter, body=doc.body) if doc else None

    def write_domain_page(self, page: Page) -> None:
        fm = dict(page.frontmatter)
        fm["scope"] = "personal"
        self._raw.put_doc(self._pages, Document(page.slug, fm, page.body))

    def delete_domain_page(self, slug: str) -> Optional[Page]:
        page = self.read_domain_page(slug)
        if page is None:
            return None
        self._raw.delete_doc(self._pages, slug)
        return page

    # ----- input scoping -----
    def list_user_ids(self) -> list[str]:
        return [self.user_id]

    def list_inbox(self, user_id: str) -> list[InboxDoc]:
        """The user's pending inbox PLUS their domain-merged docs, minus the
        ones this personal pass already consumed (marker, not a move — the
        domain librarian must still see the inbox untouched)."""
        done = set(self._store.kv_list("personal-merged"))
        out: list[InboxDoc] = []
        for coll in (_inbox(self.user_id), _merged(self.user_id)):
            for doc in self._raw.iter_docs(coll):
                if doc.doc_id in done:
                    continue
                slug = str(doc.frontmatter.get("slug") or doc.doc_id)
                out.append(InboxDoc(
                    doc.doc_id, slug, Page(doc.doc_id, doc.frontmatter, doc.body)))
        return out

    def archive_inbox(self, user_id: str, doc_id: str) -> Optional[str]:
        did = _safe_id(doc_id, "doc_id")
        self._store.kv_put(f"personal-merged/{did}", "1")
        return did
