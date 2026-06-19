"""Per-user memory store — a facade over a `DocumentStore`.

Memory is the AUTONOMOUS, per-user counterpart to the (explicit, shared)
wiki: it remembers user/session facts and useful info to reuse later. Single
user per store-scope, so consolidation is simple (no cross-user conflicts) —
the hard multi-writer machinery lives in the wiki, not here.

Two tiers, per user:
  - episodic — per-interaction fact captures (raw-ish, high volume, decays)
  - semantic — consolidated durable facts (the "useful later" knowledge)

Lifecycle (from the memory skill): draft → active → consolidated → archived,
with confidence grading and access/staleness tracking. Storage is
backend-agnostic via `adk_cc.docstore` (filesystem today; service backends by
`ADK_CC_MEMORY_STORE_URI`), so search survives a migration.

Collections: `users/<uid>/episodic`, `users/<uid>/semantic`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..docstore import Document, DocumentStore, make_document_store

# memory tiers
EPISODIC = "episodic"
SEMANTIC = "semantic"
PROCEDURAL = "procedural"

# lifecycle status
DRAFT = "draft"
ACTIVE = "active"
CONSOLIDATED = "consolidated"
ARCHIVED = "archived"


def memory_root_from_env() -> str:
    raw = os.environ.get("ADK_CC_MEMORY_ROOT")
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    base = os.environ.get("ADK_CC_WORKSPACE_ROOT") or os.getcwd()
    return os.path.join(os.path.abspath(os.path.expanduser(base)), ".memory")


def _safe_id(value: str, label: str) -> str:
    safe = "".join(c for c in value if c.isalnum() or c in "-_")
    if safe != value or not safe:
        raise ValueError(f"unsafe {label}: {value!r}")
    return safe


def _slugify(name: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


@dataclass
class MemoryItem:
    """One memory (episodic or semantic). Round-trips to a docstore Document
    (everything but `text` lives in frontmatter)."""

    id: str
    topic: str
    text: str
    memory_type: str = EPISODIC
    status: str = ACTIVE
    confidence: float = 0.5
    created: str = ""
    updated: str = ""
    sources: list[str] = field(default_factory=list)
    access_count: int = 0
    supersedes: list[str] = field(default_factory=list)

    def to_document(self) -> Document:
        fm: dict[str, Any] = {
            "topic": self.topic,
            "memory_type": self.memory_type,
            "status": self.status,
            "confidence": self.confidence,
            "created": self.created,
            "updated": self.updated,
            "access_count": self.access_count,
        }
        if self.sources:
            fm["sources"] = list(self.sources)
        if self.supersedes:
            fm["supersedes"] = list(self.supersedes)
        return Document(self.id, fm, self.text.strip() + "\n")

    @classmethod
    def from_document(cls, doc: Document) -> "MemoryItem":
        fm = doc.frontmatter or {}
        return cls(
            id=doc.doc_id,
            topic=str(fm.get("topic") or doc.doc_id),
            text=doc.body.strip(),
            memory_type=str(fm.get("memory_type") or EPISODIC),
            status=str(fm.get("status") or ACTIVE),
            confidence=float(fm.get("confidence") or 0.5),
            created=str(fm.get("created") or ""),
            updated=str(fm.get("updated") or ""),
            sources=[str(s) for s in (fm.get("sources") or [])],
            access_count=int(fm.get("access_count") or 0),
            supersedes=[str(s) for s in (fm.get("supersedes") or [])],
        )


def _episodic(user_id: str) -> str:
    return f"users/{_safe_id(user_id, 'user_id')}/episodic"


def _semantic(user_id: str) -> str:
    return f"users/{_safe_id(user_id, 'user_id')}/semantic"


class MemoryStore:
    """Per-tenant memory store; methods are per-user. Construct via
    `for_tenant`."""

    EPISODIC_OF = staticmethod(_episodic)
    SEMANTIC_OF = staticmethod(_semantic)

    def __init__(self, tenant_id: str, store: DocumentStore) -> None:
        self.tenant_id = tenant_id
        self._store = store

    @classmethod
    def for_tenant(cls, tenant_id: str, root: Optional[str] = None) -> "MemoryStore":
        tid = _safe_id(tenant_id or "local", "tenant_id")
        store = make_document_store(
            uri=os.environ.get("ADK_CC_MEMORY_STORE_URI"),
            tenant_id=tid,
            default_root=root or memory_root_from_env(),
        )
        return cls(tenant_id=tid, store=store)

    @property
    def store(self) -> DocumentStore:
        return self._store

    def list_user_ids(self) -> list[str]:
        return self._store.list_collections("users")

    # ----- episodic -----
    def add_episodic(
        self,
        user_id: str,
        text: str,
        *,
        topic: Optional[str] = None,
        sources: Optional[list[str]] = None,
        confidence: float = 0.5,
        doc_id: Optional[str] = None,
    ) -> MemoryItem:
        slug = _slugify(topic or _first_line(text)) or "note"
        if doc_id is None:
            doc_id = f"{slug}__{_short_hash(text)}"
        now = _now_iso()
        item = MemoryItem(
            id=_safe_id(doc_id, "doc_id"),
            topic=slug,
            text=text.strip(),
            memory_type=EPISODIC,
            status=ACTIVE,
            confidence=confidence,
            created=now,
            updated=now,
            sources=list(sources or []),
        )
        self._store.put_doc(_episodic(user_id), item.to_document())
        self._log(user_id, {"op": "capture", "tier": EPISODIC, "topic": slug,
                            "id": item.id, "after": item.text})
        return item

    def list_episodic(
        self, user_id: str, *, status: Optional[str] = None
    ) -> list[MemoryItem]:
        items = [MemoryItem.from_document(d) for d in self._store.iter_docs(_episodic(user_id))]
        return [i for i in items if status is None or i.status == status]

    # ----- semantic -----
    def get_semantic(self, user_id: str, topic: str) -> Optional[MemoryItem]:
        doc = self._store.get_doc(_semantic(user_id), _slugify(topic) or topic)
        return MemoryItem.from_document(doc) if doc else None

    def put_semantic(self, user_id: str, item: MemoryItem) -> None:
        item.memory_type = SEMANTIC
        if not item.updated:
            item.updated = _now_iso()
        prev = self._store.get_doc(_semantic(user_id), item.id)
        self._store.put_doc(_semantic(user_id), item.to_document())
        prev_text = prev.body.strip() if prev else None
        new_text = item.text.strip()
        if prev is None:
            op = "semantic_create"
        elif prev_text != new_text:
            op = "semantic_supersede"
        else:
            op = "semantic_corroborate"
        self._log(user_id, {"op": op, "tier": SEMANTIC, "topic": item.topic,
                            "id": item.id, "before": prev_text, "after": new_text,
                            "confidence": item.confidence})
        self._index_upsert(user_id, item)

    def list_semantic(
        self, user_id: str, *, status: Optional[str] = None
    ) -> list[MemoryItem]:
        items = [MemoryItem.from_document(d) for d in self._store.iter_docs(_semantic(user_id))]
        return [i for i in items if status is None or i.status == status]

    def set_status(self, user_id: str, tier: str, doc_id: str, status: str) -> bool:
        collection = _episodic(user_id) if tier == EPISODIC else _semantic(user_id)
        doc = self._store.get_doc(collection, doc_id)
        if doc is None:
            return False
        doc.frontmatter["status"] = status
        doc.frontmatter["updated"] = _now_iso()
        self._store.put_doc(collection, doc)
        topic = str(doc.frontmatter.get("topic") or doc_id)
        self._log(user_id, {"op": f"status:{status}", "tier": tier,
                            "id": doc_id, "topic": topic})
        if tier != EPISODIC:
            if status == ARCHIVED:
                self._index_drop(user_id, topic)
            else:
                self._index_upsert(user_id, MemoryItem.from_document(doc))
        return True

    def record_access(self, user_id: str, topic: str) -> None:
        """Bump a semantic item's access_count (recency/usefulness signal).
        Called on explicit recall, not passive injection (avoids read-path
        writes every turn)."""
        slug = _slugify(topic) or topic
        doc = self._store.get_doc(_semantic(user_id), slug)
        if doc is None:
            return
        doc.frontmatter["access_count"] = int(doc.frontmatter.get("access_count") or 0) + 1
        doc.frontmatter["last_access"] = _now_iso()
        self._store.put_doc(_semantic(user_id), doc)

    # ----- search (both tiers; semantic first) -----
    def search(
        self,
        user_id: str,
        query: str,
        *,
        tiers: tuple[str, ...] = (SEMANTIC, EPISODIC),
        limit: int = 5,
    ):
        cols = []
        for t in tiers:
            cols.append(_semantic(user_id) if t == SEMANTIC else _episodic(user_id))
        return self._store.search(cols, query, limit=limit)

    # ----- changelog (Fix G): every mutation is logged, per user -----
    def _log(self, user_id: str, entry: dict[str, Any]) -> None:
        """Append one audit record. Never raises — logging must not break a
        memory write."""
        try:
            uid = _safe_id(user_id, "user_id")
            rec = {"ts": _now_iso(), "user": uid, **entry}
            self._store.append(f"changelog/{uid}", json.dumps(rec, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass

    def read_changelog(self, user_id: str, *, limit: Optional[int] = None) -> list[dict]:
        raw = self._store.kv_get(f"changelog/{_safe_id(user_id, 'user_id')}") or ""
        out: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out[-limit:] if limit else out

    # ----- topic index (Fix G): maintained compact view for the resolver -----
    def _index_key(self, user_id: str) -> str:
        return f"topic-index/{_safe_id(user_id, 'user_id')}"

    def get_topic_index(self, user_id: str) -> dict[str, dict]:
        raw = self._store.kv_get(self._index_key(user_id))
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def _index_write(self, user_id: str, idx: dict[str, dict]) -> None:
        try:
            self._store.kv_put(self._index_key(user_id), json.dumps(idx, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass

    def _index_upsert(self, user_id: str, item: MemoryItem) -> None:
        idx = self.get_topic_index(user_id)
        idx[item.topic] = {
            "id": item.id,
            "summary": item.text.strip()[:160],
            "confidence": item.confidence,
            "status": item.status,
            "updated": item.updated,
        }
        self._index_write(user_id, idx)

    def _index_drop(self, user_id: str, topic: str) -> None:
        idx = self.get_topic_index(user_id)
        if idx.pop(topic, None) is not None:
            self._index_write(user_id, idx)

    # ----- reversibility (Fix G): undo a wrong merge/supersession -----
    def revert_semantic(self, user_id: str, topic: str) -> bool:
        """Restore a semantic topic's previous value from its supersession
        history — the rollback for a bad merge. False if there's nothing to
        revert."""
        slug = _slugify(topic) or topic
        item = self.get_semantic(user_id, slug)
        if item is None or not item.supersedes:
            return False
        prior = item.supersedes[-1]
        restored = MemoryItem(
            id=item.id, topic=item.topic, text=prior, memory_type=SEMANTIC,
            status=item.status, confidence=item.confidence,
            created=item.created, updated=_now_iso(),
            sources=item.sources, access_count=item.access_count,
            supersedes=item.supersedes[:-1],
        )
        # direct put + explicit log (bypasses put_semantic's op auto-detection)
        self._store.put_doc(_semantic(user_id), restored.to_document())
        self._log(user_id, {"op": "revert", "tier": SEMANTIC, "topic": item.topic,
                            "id": item.id, "before": item.text, "after": prior})
        self._index_upsert(user_id, restored)
        return True


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
