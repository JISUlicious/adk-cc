"""Episodic → semantic consolidation (the autonomous cron pass).

Single-user, so consolidation is simple — no cross-user conflict machinery
(that's the wiki's job). Per topic: the latest episodic capture is the current
truth (single user → latest wins over older self-statements), corroboration
across captures raises confidence, prior values move into a supersession
history, and the source episodics are marked consolidated. A staleness sweep
archives semantic facts that haven't been updated or accessed in a long time
(reversible status change, never deletion).

Deterministic by default (latest-wins synthesis); an LLM `synthesizer` can be
injected for nicer prose merges. Pure logic over the store, so it's testable
without a model — the live model is exercised in the e2e.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .store import (
    ACTIVE,
    ARCHIVED,
    CONSOLIDATED,
    DRAFT,
    MemoryItem,
    MemoryStore,
    SEMANTIC,
)

# (existing_semantic_text_or_None, [episodic_texts_newest_first]) -> merged text
Synthesizer = Callable[[Optional[str], list[str]], str]


@dataclass
class ConsolidationReport:
    user_id: str
    episodic_seen: int = 0
    topics_consolidated: int = 0
    created: int = 0
    updated: int = 0
    archived_stale: int = 0
    pruned_episodic: int = 0
    topics: list[str] = field(default_factory=list)


def _episodic_cap() -> int:
    """Max CONSOLIDATED episodics to retain per user; 0/unset = keep all.
    Prevents the episodic tier (#5) from growing unbounded — older ones are
    reversibly archived (provenance survives in the semantic item's sources)."""
    try:
        return max(0, int(os.environ.get("ADK_CC_MEMORY_EPISODIC_CAP", "")))
    except ValueError:
        return 0


def _default_synth(existing: Optional[str], episodic_newest_first: list[str]) -> str:
    """Latest capture is the current statement (single-user: newest wins)."""
    return episodic_newest_first[0] if episodic_newest_first else (existing or "")


def _confidence(n_support: int, had_existing: bool) -> float:
    base = 0.5 + 0.1 * max(0, n_support - 1) + (0.1 if had_existing else 0.0)
    return round(min(0.95, base), 2)


def _age_days(iso_ts: str, now_epoch: float) -> float:
    try:
        t = time.mktime(time.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, (now_epoch - t) / 86400.0)


def consolidate_user(
    store: MemoryStore,
    user_id: str,
    *,
    synthesizer: Optional[Synthesizer] = None,
    stale_days: int = 90,
    now_epoch: Optional[float] = None,
) -> ConsolidationReport:
    """Fold a user's fresh episodic memories into semantic facts, then sweep
    stale semantic items to archived."""
    synth = synthesizer or _default_synth
    now = now_epoch if now_epoch is not None else time.time()
    report = ConsolidationReport(user_id=user_id)

    # 1. cluster fresh episodic by topic (newest first within a topic)
    fresh = [
        i for i in store.list_episodic(user_id) if i.status in (ACTIVE, DRAFT)
    ]
    report.episodic_seen = len(fresh)
    clusters: dict[str, list[MemoryItem]] = {}
    for item in fresh:
        clusters.setdefault(item.topic, []).append(item)
    for items in clusters.values():
        items.sort(key=lambda i: i.created, reverse=True)

    # 2. merge each topic into its semantic item
    for topic, items in sorted(clusters.items()):
        existing = store.get_semantic(user_id, topic)
        texts = [i.text for i in items]  # newest first
        merged = synth(existing.text if existing else None, texts).strip()
        supersedes = list(existing.supersedes) if existing else []
        # keep superseded history: older captures in THIS batch that differ
        # from the chosen current value, plus a differing prior semantic.
        for older in texts[1:] + ([existing.text] if existing else []):
            o = older.strip()
            if o and o != merged and o not in supersedes:
                supersedes.append(o)
        # union of source refs
        srcs: list[str] = list(existing.sources) if existing else []
        for it in items:
            for s in it.sources:
                if s not in srcs:
                    srcs.append(s)
        n_support = len(items) + (1 if existing else 0)
        store.put_semantic(
            user_id,
            MemoryItem(
                id=topic,
                topic=topic,
                text=merged,
                memory_type=SEMANTIC,
                status=CONSOLIDATED if existing else ACTIVE,
                confidence=_confidence(n_support, existing is not None),
                created=existing.created if existing else _now_iso(now),
                updated=_now_iso(now),
                sources=srcs,
                supersedes=supersedes,
                access_count=existing.access_count if existing else 0,
            ),
        )
        for it in items:
            store.set_status(user_id, "episodic", it.id, CONSOLIDATED)
        report.topics_consolidated += 1
        report.topics.append(topic)
        if existing:
            report.updated += 1
        else:
            report.created += 1

    # 3. staleness sweep: old + unused semantic → archived (reversible)
    for sem in store.list_semantic(user_id, status=ACTIVE):
        ref = sem.updated or sem.created
        if _age_days(ref, now) > stale_days and sem.access_count == 0:
            store.set_status(user_id, SEMANTIC, sem.id, ARCHIVED)
            report.archived_stale += 1

    # 4. prune (#5): keep only the most recent CONSOLIDATED episodics per user;
    # archive the rest (reversible — provenance lives on in semantic.sources).
    cap = _episodic_cap()
    if cap:
        done = sorted(
            store.list_episodic(user_id, status=CONSOLIDATED),
            key=lambda i: i.updated or i.created,
        )
        for old in done[: max(0, len(done) - cap)]:
            store.set_status(user_id, "episodic", old.id, ARCHIVED)
            report.pruned_episodic += 1

    return report


def _now_iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# Process-global lock serializing the two IN-PROCESS consolidation callers (the
# periodic scheduler and the capture-path threshold trigger) so they never
# read-modify-write the same user's files concurrently. The cron / unit tests
# call consolidate_user/all directly in their own process — uncontended there.
consolidation_lock = threading.Lock()


def pending_episodic_count(store: "MemoryStore", user_id: str) -> int:
    """Episodic captures not yet folded into semantic memory (status ACTIVE or
    DRAFT) — the same predicate consolidate_user promotes. This is the count a
    threshold trigger checks ("≥N unprocessed stacked → consolidate now")."""
    return sum(1 for i in store.list_episodic(user_id) if i.status in (ACTIVE, DRAFT))


def discover_tenants(root: str) -> list[str]:
    """Tenant ids with memory under `root` (dirs that contain a `users/`)."""
    if not os.path.isdir(root):
        return []
    return sorted(
        name for name in os.listdir(root)
        if os.path.isdir(os.path.join(root, name, "users"))
    )


def consolidate_all(
    root: str,
    *,
    tenants: Optional[list[str]] = None,
    synthesizer: Optional[Synthesizer] = None,
    stale_days: int = 90,
) -> list[tuple[str, ConsolidationReport]]:
    """Run `consolidate_user` for every (tenant, user) under `root`.

    The single walk shared by the CLI cron (scripts/memory_consolidator.py) and
    the optional in-process server scheduler, so both enumerate tenants/users
    identically. `tenants=None` → discover from the filesystem. Returns
    (tenant_id, report) pairs in iteration order; callers do their own logging.
    """
    selected = tenants if tenants is not None else discover_tenants(root)
    out: list[tuple[str, ConsolidationReport]] = []
    for tenant in selected:
        store = MemoryStore.for_tenant(tenant, root=root)
        for user_id in store.list_user_ids():
            out.append((
                tenant,
                consolidate_user(
                    store, user_id, synthesizer=synthesizer, stale_days=stale_days
                ),
            ))
    return out
