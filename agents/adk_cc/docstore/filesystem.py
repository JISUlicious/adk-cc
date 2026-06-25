"""Filesystem-backed DocumentStore — the default backend.

Documents are markdown files under `<base>/<collection>/<doc_id>.md`;
control-plane KV lives under `<base>/_kv/<key>` (a separate subtree, so the
document search never sees it). Search is in-process lexical scoring (token
overlap, title-weighted) — the same algorithm the wiki/memory layers used
before, relocated here so it travels with the store contract. A service
backend (SQLite FTS, OpenSearch, a vector DB) implements `search` natively and
swaps in with no caller changes.

Concurrency: writes are atomic per file (temp + os.replace), but there is NO
cross-process lock around read-modify-write sequences (index/settings/changelog
updates). This is safe for the intended deployment — a SINGLE uvicorn worker
(the in-memory pacing/credential/consolidation state already assumes one
process) plus the single-writer librarian per tenant domain. Running
`uvicorn --workers >1` against this store risks lost updates; use a
service-backed DocumentStore (or add a per-key filelock) for multi-worker.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterator, Optional, Sequence

import yaml

from .base import Document, DocumentStore, Hit

_FENCE = "---"
_CLOSE_FENCE_RE = re.compile(r"^---[ \t]*$", re.MULTILINE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _safe_segment(value: str, label: str) -> str:
    safe = "".join(c for c in value if c.isalnum() or c in "-_")
    if safe != value or not safe:
        raise ValueError(f"unsafe {label} for filesystem path: {value!r}")
    return safe


def _safe_path(base: str, namespaced: str, label: str) -> str:
    """Resolve a `/`-separated logical name under base, sanitizing each
    segment (path-traversal guard, mirrors service/tenancy._safe_id)."""
    parts = [_safe_segment(p, label) for p in namespaced.split("/") if p]
    if not parts:
        raise ValueError(f"empty {label}")
    return os.path.join(base, *parts)


def _serialize(doc: Document) -> str:
    body = doc.body.rstrip() + "\n"
    if not doc.frontmatter:
        return body
    fm = yaml.safe_dump(
        doc.frontmatter, sort_keys=True, allow_unicode=True, default_flow_style=False
    ).strip()
    return f"{_FENCE}\n{fm}\n{_FENCE}\n\n{body}"


def _parse(text: str, doc_id: str) -> Document:
    if text.startswith(_FENCE + "\n") or text.startswith(_FENCE + "\r\n"):
        rest = text.split("\n", 1)[1] if "\n" in text else ""
        m = _CLOSE_FENCE_RE.search(rest)
        if m is not None:
            fm: dict[str, Any] = {}
            try:
                loaded = yaml.safe_load(rest[: m.start()])
                if isinstance(loaded, dict):
                    fm = loaded
            except yaml.YAMLError:
                fm = {}
            return Document(doc_id=doc_id, frontmatter=fm, body=rest[m.end():].lstrip("\n"))
    return Document(doc_id=doc_id, frontmatter={}, body=text)


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ---- lexical scoring (relocated from the wiki search layer, unchanged) ----
def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _title_of(doc: Document) -> str:
    t = doc.frontmatter.get("title")
    if isinstance(t, str) and t.strip():
        return t.strip()
    m = _H1_RE.search(doc.body)
    return m.group(1).strip() if m else doc.doc_id


def _score(query_tokens: set[str], doc: Document) -> float:
    if not query_tokens:
        return 0.0
    title_toks = set(_tokens(_title_of(doc)))
    body_toks = _tokens(doc.body)
    body_set = set(body_toks)
    body_freq: dict[str, int] = {}
    for t in body_toks:
        body_freq[t] = body_freq.get(t, 0) + 1
    covered = query_tokens & (title_toks | body_set)
    if not covered:
        return 0.0
    coverage = len(covered) / len(query_tokens)
    title_hits = len(query_tokens & title_toks)
    body_hits = sum(min(body_freq.get(t, 0), 3) for t in query_tokens)
    return coverage * 10 + title_hits * 3 + body_hits


def _snippet(doc: Document, query_tokens: set[str], width: int = 220) -> str:
    body = doc.body.strip()
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


class FilesystemDocumentStore(DocumentStore):
    def __init__(self, base_path: str) -> None:
        self.base = os.path.abspath(os.path.expanduser(base_path))

    # ----- documents -----
    def _doc_path(self, collection: str, doc_id: str) -> str:
        return _safe_path(
            self.base, f"{collection}/{_safe_segment(doc_id, 'doc_id')}", "collection"
        ) + ".md"

    def put_doc(self, collection: str, doc: Document) -> None:
        _atomic_write(self._doc_path(collection, doc.doc_id), _serialize(doc))

    def get_doc(self, collection: str, doc_id: str) -> Optional[Document]:
        path = self._doc_path(collection, doc_id)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return _parse(fh.read(), doc_id)

    def delete_doc(self, collection: str, doc_id: str) -> bool:
        path = self._doc_path(collection, doc_id)
        if not os.path.isfile(path):
            return False
        os.remove(path)
        return True

    def list_ids(self, collection: str) -> list[str]:
        d = _safe_path(self.base, collection, "collection")
        if not os.path.isdir(d):
            return []
        return sorted(f[:-3] for f in os.listdir(d) if f.endswith(".md"))

    def iter_docs(self, collection: str) -> Iterator[Document]:
        for doc_id in self.list_ids(collection):
            doc = self.get_doc(collection, doc_id)
            if doc is not None:
                yield doc

    def list_collections(self, prefix: str) -> list[str]:
        d = _safe_path(self.base, prefix, "collection")
        if not os.path.isdir(d):
            return []
        return sorted(
            name for name in os.listdir(d) if os.path.isdir(os.path.join(d, name))
        )

    def move_doc(self, src_collection: str, doc_id: str, dst_collection: str) -> bool:
        src = self._doc_path(src_collection, doc_id)
        if not os.path.isfile(src):
            return False
        dst = self._doc_path(dst_collection, doc_id)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
        return True

    def search(
        self, collections: Sequence[str], query: str, *, limit: int = 10
    ) -> list[Hit]:
        q = set(_tokens(query))
        hits: list[Hit] = []
        for collection in collections:
            for doc in self.iter_docs(collection):
                s = _score(q, doc)
                if s > 0:
                    hits.append(
                        Hit(collection, doc.doc_id, s, _snippet(doc, q), doc.frontmatter)
                    )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    # ----- control-plane KV -----
    def _kv_path(self, key: str) -> str:
        return _safe_path(self.base, f"_kv/{key}", "kv key")

    def kv_get(self, key: str) -> Optional[str]:
        path = self._kv_path(key)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def kv_put(self, key: str, value: str) -> None:
        _atomic_write(self._kv_path(key), value)

    def kv_delete(self, key: str) -> bool:
        path = self._kv_path(key)
        if not os.path.isfile(path):
            return False
        os.remove(path)
        return True

    def kv_list(self, prefix: str) -> list[str]:
        d = _safe_path(self.base, f"_kv/{prefix}", "kv key")
        if not os.path.isdir(d):
            return []
        return sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))

    def append(self, key: str, line: str) -> None:
        path = self._kv_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")
