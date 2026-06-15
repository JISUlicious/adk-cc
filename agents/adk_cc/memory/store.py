"""Tenant-scoped filesystem store for the LLM-wiki memory system.

Layout (one tree per tenant; `domain == tenant`, per the requirement that
same-domain users share a wiki and separate domains separate by tenant):

    <ADK_CC_WIKI_ROOT>/<tenant_id>/
      domain/                    # SHARED — the librarian is the ONLY writer
        wiki/  <slug>.md, index.md
        sources/                 # immutable ingested originals (provenance)
        schema.md                # the wiki's own conventions doc
        .changelog/log.jsonl     # append-only merge log
      users/<user_id>/
        inbox/                   # user-scope captures awaiting merge
        merged/                  # archived post-merge (user keeps a copy)
      .quarantine/               # conflicted claims awaiting adjudication
      .resolutions/              # sticky resolutions keyed by claim-hash
      settings.json             # per-tenant admin-tunable knobs

This store is a HOST-side service store (like credentials / skills /
mcp-server registries), NOT the user's sandboxed workspace — it is read
and written directly via Python IO, never through the sandbox backend.

The store is deliberately dumb: paths, atomic IO, and the inbox→merged
lifecycle. All semantics (search, recall, conflict classification, merge)
live in sibling modules so this file stays easy to reason about and test.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from . import page as pagelib
from .page import Page

# --- defaults the admin panel can override per-tenant (settings.json) ---
_DEFAULT_CORROBORATION_N = 2


def _safe_id(value: str, label: str) -> str:
    """Reject path-traversal before an id reaches os.path.join. Mirrors
    `service/tenancy._safe_id` — duplicated (4 lines) so the memory package
    imports nothing from the service layer."""
    safe = "".join(c for c in value if c.isalnum() or c in "-_")
    if safe != value or not safe:
        raise ValueError(f"unsafe {label} for filesystem path: {value!r}")
    return safe


def wiki_root_from_env() -> str:
    """Resolve the wiki storage root. Explicit `ADK_CC_WIKI_ROOT` wins;
    otherwise a `.wiki` sibling of the workspace root (or CWD in dev)."""
    raw = os.environ.get("ADK_CC_WIKI_ROOT")
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    base = os.environ.get("ADK_CC_WORKSPACE_ROOT") or os.getcwd()
    return os.path.join(os.path.abspath(os.path.expanduser(base)), ".wiki")


def corroboration_default_from_env() -> int:
    """Env fallback for the corroboration threshold; admin settings.json
    overrides this per tenant (see `WikiStore.corroboration_n`)."""
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
    """A user-scope capture awaiting merge. `doc_id` is the filename stem
    (unique); `slug` is the topic it concerns (may collide across docs)."""

    doc_id: str
    slug: str
    page: Page


@dataclass
class WikiStore:
    """All paths + IO for one tenant's wiki tree. Construct via
    `for_tenant`; call `ensure()` once before first use."""

    tenant_id: str
    root: str  # <ADK_CC_WIKI_ROOT>/<tenant_id>

    @classmethod
    def for_tenant(cls, tenant_id: str, root: Optional[str] = None) -> "WikiStore":
        tid = _safe_id(tenant_id or "local", "tenant_id")
        base = root or wiki_root_from_env()
        return cls(tenant_id=tid, root=os.path.join(base, tid))

    # ----- path accessors -----
    @property
    def domain_dir(self) -> str:
        return os.path.join(self.root, "domain")

    @property
    def wiki_dir(self) -> str:
        return os.path.join(self.domain_dir, "wiki")

    @property
    def sources_dir(self) -> str:
        return os.path.join(self.domain_dir, "sources")

    @property
    def schema_path(self) -> str:
        return os.path.join(self.domain_dir, "schema.md")

    @property
    def index_path(self) -> str:
        return os.path.join(self.wiki_dir, "index.md")

    @property
    def changelog_path(self) -> str:
        return os.path.join(self.domain_dir, ".changelog", "log.jsonl")

    @property
    def quarantine_dir(self) -> str:
        return os.path.join(self.root, ".quarantine")

    @property
    def resolutions_dir(self) -> str:
        return os.path.join(self.root, ".resolutions")

    @property
    def settings_path(self) -> str:
        return os.path.join(self.root, "settings.json")

    def user_dir(self, user_id: str) -> str:
        return os.path.join(self.root, "users", _safe_id(user_id, "user_id"))

    def inbox_dir(self, user_id: str) -> str:
        return os.path.join(self.user_dir(user_id), "inbox")

    def merged_dir(self, user_id: str) -> str:
        return os.path.join(self.user_dir(user_id), "merged")

    # ----- bring-up -----
    def ensure(self) -> "WikiStore":
        """Create the tenant skeleton (idempotent) + seed schema/index."""
        for d in (
            self.wiki_dir,
            self.sources_dir,
            os.path.dirname(self.changelog_path),
            self.quarantine_dir,
            self.resolutions_dir,
        ):
            os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.schema_path):
            _atomic_write(self.schema_path, _DEFAULT_SCHEMA)
        if not os.path.exists(self.index_path):
            _atomic_write(self.index_path, "# Index\n\n_(empty — no pages yet)_\n")
        return self

    # ----- domain pages (read by everyone; written only by librarian) -----
    def list_domain_pages(self) -> list[str]:
        if not os.path.isdir(self.wiki_dir):
            return []
        return sorted(
            f[:-3]
            for f in os.listdir(self.wiki_dir)
            if f.endswith(".md") and f != "index.md"
        )

    def read_domain_page(self, slug: str) -> Optional[Page]:
        path = os.path.join(self.wiki_dir, _safe_id(slug, "slug") + ".md")
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return pagelib.parse(fh.read(), slug)

    def write_domain_page(self, page: Page) -> str:
        """Atomically write/replace a domain page (librarian only)."""
        os.makedirs(self.wiki_dir, exist_ok=True)
        path = os.path.join(self.wiki_dir, _safe_id(page.slug, "slug") + ".md")
        _atomic_write(path, pagelib.serialize(page))
        return path

    def read_index(self) -> str:
        if not os.path.isfile(self.index_path):
            return ""
        with open(self.index_path, "r", encoding="utf-8") as fh:
            return fh.read()

    def write_index(self, text: str) -> None:
        os.makedirs(self.wiki_dir, exist_ok=True)
        _atomic_write(self.index_path, text)

    def read_schema(self) -> str:
        if not os.path.isfile(self.schema_path):
            return ""
        with open(self.schema_path, "r", encoding="utf-8") as fh:
            return fh.read()

    # ----- user inbox (user-scope captures) -----
    def add_inbox(
        self,
        user_id: str,
        text: str,
        *,
        title: Optional[str] = None,
        topic: Optional[str] = None,
        sources: Optional[list[str]] = None,
        extra_frontmatter: Optional[dict[str, Any]] = None,
        doc_id: Optional[str] = None,
    ) -> InboxDoc:
        """Capture a doc/claim into the user's inbox. The slug comes from
        `topic` else `title` else the first body line. `doc_id` is unique
        (`<slug>__<hash8>`) so repeated captures of one topic don't clobber.
        Idempotent when an explicit `doc_id` is reused."""
        slug = pagelib.slugify(topic or title or _first_line(text)) or "note"
        if doc_id is None:
            doc_id = f"{slug}__{_short_hash(text)}"
        doc_id = _safe_id(doc_id, "doc_id")
        fm: dict[str, Any] = {
            "title": title or _first_line(text) or slug,
            "slug": slug,
            "captured_by": user_id,
            "created": _now_iso(),
        }
        if sources:
            fm["sources"] = list(sources)
        if extra_frontmatter:
            fm.update(extra_frontmatter)
        page = Page(slug=slug, frontmatter=fm, body=text.strip() + "\n")
        os.makedirs(self.inbox_dir(user_id), exist_ok=True)
        path = os.path.join(self.inbox_dir(user_id), doc_id + ".md")
        _atomic_write(path, pagelib.serialize(page))
        return InboxDoc(doc_id=doc_id, slug=slug, page=page)

    def list_inbox(self, user_id: str) -> list[InboxDoc]:
        d = self.inbox_dir(user_id)
        if not os.path.isdir(d):
            return []
        out: list[InboxDoc] = []
        for f in sorted(os.listdir(d)):
            if not f.endswith(".md"):
                continue
            with open(os.path.join(d, f), "r", encoding="utf-8") as fh:
                page = pagelib.parse(fh.read(), f[:-3])
            slug = str(page.frontmatter.get("slug") or page.slug)
            out.append(InboxDoc(doc_id=f[:-3], slug=slug, page=page))
        return out

    def list_user_ids(self) -> list[str]:
        users = os.path.join(self.root, "users")
        if not os.path.isdir(users):
            return []
        return sorted(
            u for u in os.listdir(users) if os.path.isdir(os.path.join(users, u))
        )

    def archive_inbox(self, user_id: str, doc_id: str) -> Optional[str]:
        """Move a processed inbox doc → merged/ (the user keeps the copy).
        Returns the new path, or None if the source was already gone."""
        doc_id = _safe_id(doc_id, "doc_id")
        src = os.path.join(self.inbox_dir(user_id), doc_id + ".md")
        if not os.path.isfile(src):
            return None
        os.makedirs(self.merged_dir(user_id), exist_ok=True)
        dst = os.path.join(self.merged_dir(user_id), doc_id + ".md")
        os.replace(src, dst)
        return dst

    # ----- sources (immutable provenance) -----
    def write_source(self, source_id: str, text: str) -> str:
        os.makedirs(self.sources_dir, exist_ok=True)
        path = os.path.join(self.sources_dir, _safe_id(source_id, "source_id") + ".md")
        if not os.path.exists(path):  # immutable: first write wins
            _atomic_write(path, text)
        return path

    def has_source(self, source_id: str) -> bool:
        return os.path.isfile(
            os.path.join(self.sources_dir, _safe_id(source_id, "source_id") + ".md")
        )

    # ----- changelog -----
    def append_changelog(self, entry: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.changelog_path), exist_ok=True)
        rec = {"ts": _now_iso(), **entry}
        with open(self.changelog_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ----- sticky resolutions (idempotency + human adjudication) -----
    def get_sticky(self, claim_hash: str) -> Optional[dict[str, Any]]:
        """A prior resolution record for a claim-hash, or None. `by` is
        'auto' (this layer recorded a hold) or 'human' (admin adjudicated)."""
        path = os.path.join(self.resolutions_dir, _safe_id(claim_hash, "hash") + ".json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError):
            return None

    def set_sticky(
        self, claim_hash: str, *, action: str, by: str = "auto", note: str = ""
    ) -> None:
        os.makedirs(self.resolutions_dir, exist_ok=True)
        path = os.path.join(self.resolutions_dir, _safe_id(claim_hash, "hash") + ".json")
        _atomic_write(
            path,
            json.dumps(
                {"action": action, "by": by, "note": note, "ts": _now_iso()}, indent=2
            ) + "\n",
        )

    def human_override(self, claim_hash: str) -> Optional[str]:
        """'accept'/'reject' if a HUMAN adjudicated this claim, else None.
        Auto-recorded holds do NOT count as overrides — only human ones do."""
        rec = self.get_sticky(claim_hash)
        if rec and rec.get("by") == "human":
            action = rec.get("action")
            if action in ("accept", "reject"):
                return action
        return None

    # ----- quarantine (human review queue) -----
    def add_quarantine(self, claim_hash: str, record: dict[str, Any]) -> str:
        """Queue a conflicted/uncited claim for human review. Keyed by
        claim-hash so re-running the merge doesn't pile up duplicate notes."""
        os.makedirs(self.quarantine_dir, exist_ok=True)
        qid = _safe_id(claim_hash, "hash")
        path = os.path.join(self.quarantine_dir, qid + ".json")
        rec = {"claim_hash": claim_hash, "status": "pending", "ts": _now_iso(), **record}
        _atomic_write(path, json.dumps(rec, indent=2) + "\n")
        return qid

    def list_quarantine(self, *, pending_only: bool = True) -> list[dict[str, Any]]:
        if not os.path.isdir(self.quarantine_dir):
            return []
        out: list[dict[str, Any]] = []
        for f in sorted(os.listdir(self.quarantine_dir)):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.quarantine_dir, f), encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            if pending_only and rec.get("status") != "pending":
                continue
            out.append(rec)
        return out

    def is_quarantined(self, claim_hash: str) -> bool:
        path = os.path.join(self.quarantine_dir, _safe_id(claim_hash, "hash") + ".json")
        return os.path.isfile(path)

    # ----- per-tenant settings (admin-tunable) -----
    def read_settings(self) -> dict[str, Any]:
        if not os.path.isfile(self.settings_path):
            return {}
        try:
            with open(self.settings_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def write_settings(self, settings: dict[str, Any]) -> None:
        _atomic_write(self.settings_path, json.dumps(settings, indent=2) + "\n")

    def set_setting(self, key: str, value: Any) -> dict[str, Any]:
        s = self.read_settings()
        s[key] = value
        self.write_settings(s)
        return s

    @property
    def corroboration_n(self) -> int:
        """How many independent users must corroborate a claim to overturn a
        domain fact without human adjudication. Admin settings.json wins;
        else the env default (`ADK_CC_WIKI_CORROBORATION_N`, then 2)."""
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
def _atomic_write(path: str, text: str) -> None:
    """Write via a temp file + os.replace so a reader never sees a partial
    page. Per-file atomic; the single-writer librarian makes cross-page
    races a non-issue."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


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
