"""Autonomous per-user memory for adk-cc.

The autonomous counterpart to the (explicit, shared) `adk_cc.wiki`: it
remembers user/session facts and useful info to reuse later, capturing and
recalling on its own. Single-user per scope, so consolidation is simple
(latest-wins + corroboration), unlike the wiki's multi-writer conflict model.

Two tiers — episodic (raw captures) and semantic (consolidated durable
facts) — with the memory-skill lifecycle (draft→active→consolidated→archived),
confidence grading, and access/staleness tracking. Storage is backend-agnostic
via `adk_cc.docstore` (`ADK_CC_MEMORY_STORE_URI`).

Layers:
  - `store`       — MemoryStore facade + MemoryItem (episodic/semantic tiers).
  - `recall`      — budgeted always-injected recall block.
  - `consolidate` — episodic→semantic merge + staleness sweep (cron pass).

Surfaced as: a capture+recall plugin (autonomous) and a consolidation cron.
Gated by `ADK_CC_MEMORY=1`.
"""

from __future__ import annotations

from .consolidate import (
    ConsolidationReport,
    consolidate_all,
    consolidate_user,
    consolidation_lock,
    discover_tenants,
    pending_episodic_count,
)
from .principal import get_principal, set_principal
from .recall import recall_context
from .resolve import Resolution, compact_all, compact_user, resolve_facts
from .synth import make_llm_synthesizer
from .store import (
    ACTIVE,
    ARCHIVED,
    CONSOLIDATED,
    DRAFT,
    EPISODIC,
    PROCEDURAL,
    SEMANTIC,
    MemoryItem,
    MemoryStore,
    memory_root_from_env,
)

__all__ = [
    "MemoryStore",
    "MemoryItem",
    "memory_root_from_env",
    "recall_context",
    "get_principal",
    "set_principal",
    "resolve_facts",
    "Resolution",
    "compact_user",
    "compact_all",
    "make_llm_synthesizer",
    "consolidate_user",
    "consolidate_all",
    "consolidation_lock",
    "pending_episodic_count",
    "discover_tenants",
    "ConsolidationReport",
    "EPISODIC",
    "SEMANTIC",
    "PROCEDURAL",
    "DRAFT",
    "ACTIVE",
    "CONSOLIDATED",
    "ARCHIVED",
]
