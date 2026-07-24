"""Fix A + D: identity resolution for memory capture.

For each newly extracted fact, decide whether it CORROBORATEs / UPDATEs an
existing memory topic or is NEW — so the same fact captured under drifting
slugs folds onto ONE topic at write time (instead of fragmenting, then maybe
merging later). Identity is decided by the model reasoning over the user's
existing topics (content), not by exact slug match.

Fix D: every proposed merge (corroborate/update) is verified by a second,
independent model check before it's trusted — a wrong merge corrupts data via
latest-wins, so we gate it. On disagreement, downgrade to NEW (prefer a missed
merge over a false one).

Reliability: any model failure falls back to deterministic slug canonicalization
(memory/canonicalize.py), so capture never breaks. Disable via
ADK_CC_MEMORY_RESOLVE=0 (then capture behaves as before: slug as-is).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .canonicalize import deterministic_canonical
from .store import MemoryStore, _slugify
from ..config.schema import env_bool

NEW, CORROBORATE, UPDATE = "new", "corroborate", "update"


@dataclass
class Resolution:
    fact: str
    topic: str            # canonical topic slug the episodic should be filed under
    action: str           # new | corroborate | update
    proposed: str         # the slug the capture model originally suggested
    verified: bool = True
    reason: str = ""


def _enabled() -> bool:
    return env_bool("ADK_CC_MEMORY_RESOLVE", True)


def _verify_enabled() -> bool:
    return env_bool("ADK_CC_MEMORY_RESOLVE_VERIFY", True)


def _existing_view(store: MemoryStore, user_id: str) -> dict[str, str]:
    """Topic -> short summary, from the maintained semantic index (Fix G) PLUS
    current episodic topics — so dedup works WITHIN a session, before any
    consolidation has produced semantic items."""
    view: dict[str, str] = {}
    for topic, meta in store.get_topic_index(user_id).items():
        view[topic] = str((meta or {}).get("summary") or topic)
    for e in store.list_episodic(user_id):
        view.setdefault(e.topic, e.text.strip()[:160])
    return view


_RESOLVE_PROMPT = (
    "You maintain a user's long-term memory. Below are the user's EXISTING "
    "memory topics and NEW facts just extracted from a conversation. For each "
    "NEW fact, decide whether it is about the SAME underlying subject as an "
    "existing topic.\n\n"
    "Output ONE line per new fact, EXACTLY:\n"
    "<n>: NEW\n"
    "<n>: CORROBORATE <existing-topic>   (same subject, same value)\n"
    "<n>: UPDATE <existing-topic>        (same subject, the value changed)\n"
    "Match ONLY when it is clearly the same subject. When unsure, say NEW.\n\n"
    "EXISTING TOPICS:\n{existing}\n\nNEW FACTS:\n{new}"
)

_VERIFY_PROMPT = (
    "A memory system wants to merge a new fact into an existing topic. Are they "
    "about the SAME underlying subject, so merging is correct?\n\n"
    "EXISTING [{topic}]: {summary}\n"
    "NEW: {fact}\n\n"
    "Answer EXACTLY 'YES' or 'NO'."
)


async def _generate(model, prompt: str) -> str:
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    req = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        config=types.GenerateContentConfig(),
    )
    # double-yield-safe collector (see llm_text.py)
    from .llm_text import final_response_text
    return await final_response_text(model, req)


def _parse(raw: str, facts: list[tuple[str, str]], existing: dict[str, str]) -> list[Resolution]:
    # default every fact to NEW (slugged proposed topic); override from output
    res = [
        Resolution(fact=f, topic=_slugify(t) or "note", action=NEW, proposed=_slugify(t) or "note")
        for t, f in facts
    ]
    for line in (raw or "").splitlines():
        s = line.strip().lstrip("-* ").strip()
        if ":" not in s:
            continue
        head, _, rest = s.partition(":")
        try:
            n = int(head.strip()) - 1
        except ValueError:
            continue
        if not (0 <= n < len(res)):
            continue
        rest = rest.strip()
        upper = rest.upper()
        if upper.startswith("CORROBORATE") or upper.startswith("UPDATE"):
            verb = CORROBORATE if upper.startswith("CORROBORATE") else UPDATE
            target = _slugify(rest.split(None, 1)[1]) if len(rest.split(None, 1)) > 1 else ""
            if target and target in existing:
                res[n].action = verb
                res[n].topic = target
            # named a non-existent topic → leave as NEW (safety)
    return res


async def _verify(model, r: Resolution, existing: dict[str, str]) -> Resolution:
    if r.action == NEW:
        return r
    try:
        ans = await _generate(model, _VERIFY_PROMPT.format(
            topic=r.topic, summary=existing.get(r.topic, ""), fact=r.fact))
        if "YES" in ans.strip().upper().split():
            return r
        # rejected → don't merge; file under the proposed (new) slug
        r.action, r.topic, r.verified, r.reason = NEW, r.proposed, False, "verify_rejected"
    except Exception:  # noqa: BLE001 — verify failure ⇒ be conservative, don't merge
        r.action, r.topic, r.verified, r.reason = NEW, r.proposed, False, "verify_error"
    return r


def _deterministic_resolve(facts: list[tuple[str, str]], existing: dict[str, str]) -> list[Resolution]:
    """No-model fallback: map each proposed slug onto an existing topic when
    token-clustering says they're the same; else NEW."""
    existing_topics = list(existing)
    out: list[Resolution] = []
    for topic, fact in facts:
        proposed = _slugify(topic) or "note"
        mapping = deterministic_canonical(existing_topics + [proposed])
        canon = mapping.get(proposed, proposed)
        # if the proposed slug clustered onto an existing topic, treat as merge
        match = next((t for t in existing_topics if mapping.get(t) == canon), None)
        if match:
            out.append(Resolution(fact, match, UPDATE, proposed, reason="deterministic"))
        else:
            out.append(Resolution(fact, proposed, NEW, proposed, reason="deterministic"))
    return out


async def resolve_facts(
    model, store: MemoryStore, user_id: str, facts: list[tuple[str, str]]
) -> list[Resolution]:
    """Resolve each (proposed_topic, fact) to the topic it should be filed
    under. LLM-driven with verify (Fix A+D); deterministic fallback."""
    if not facts:
        return []
    existing = _existing_view(store, user_id)
    # nothing to merge against, disabled, or no model → deterministic (identity
    # when `existing` is empty, i.e. exactly the old behavior on first capture).
    if not existing or model is None or not _enabled():
        return _deterministic_resolve(facts, existing)
    try:
        listing = "\n".join(f"- {t}: {s}" for t, s in existing.items())
        new = "\n".join(f"{i+1}. {f}" for i, (_, f) in enumerate(facts))
        raw = await _generate(model, _RESOLVE_PROMPT.format(existing=listing, new=new))
        res = _parse(raw, facts, existing)
    except Exception:  # noqa: BLE001
        return _deterministic_resolve(facts, existing)
    if _verify_enabled():
        res = [await _verify(model, r, existing) for r in res]
    return res


# --------------------------------------------------------------------------
# Fix F: periodic LLM compaction — re-merge residual fragmentation across a
# user's whole SEMANTIC tier (drift that slipped past the per-write resolver).
# --------------------------------------------------------------------------
def _verify_same_sync(model, topic: str, summary: str, fact: str) -> bool:
    import asyncio
    try:
        ans = asyncio.run(_generate(model, _VERIFY_PROMPT.format(
            topic=topic, summary=summary, fact=fact)))
        return "YES" in ans.strip().upper().split()
    except Exception:  # noqa: BLE001 — conservative: don't merge if unsure
        return False


def compact_user(model, store: MemoryStore, user_id: str, *, verify: bool = True) -> dict:
    """Merge semantically-equivalent semantic topics for one user. The survivor
    keeps the latest value; merged-away items' values go to `supersedes` and
    they're archived (reversible). Verified per merge (Fix D). No-op without a
    model or with <2 topics."""
    from .canonicalize import make_llm_canonical
    from .store import ACTIVE, ARCHIVED, SEMANTIC

    sems = store.list_semantic(user_id, status=ACTIVE)
    if model is None or len(sems) < 2:
        return {"merged": 0, "groups": 0}
    by_topic = {s.topic: s for s in sems}
    try:
        mapping = make_llm_canonical(model)(list(by_topic))
    except Exception:  # noqa: BLE001
        return {"merged": 0, "groups": 0}

    groups: dict[str, list[str]] = {}
    for t, canon in mapping.items():
        if t in by_topic:
            groups.setdefault(canon, []).append(t)

    merged = ngroups = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda t: (by_topic[t].confidence, by_topic[t].updated), reverse=True)
        survivor = by_topic[members[0]]
        to_merge = [
            by_topic[m] for m in members[1:]
            if not verify or _verify_same_sync(model, survivor.topic, survivor.text, by_topic[m].text)
        ]
        if not to_merge:
            continue
        ngroups += 1
        sup, srcs = list(survivor.supersedes), list(survivor.sources)
        for other in to_merge:
            if other.text and other.text != survivor.text and other.text not in sup:
                sup.append(other.text)
            for s in other.sources:
                if s not in srcs:
                    srcs.append(s)
            store.set_status(user_id, SEMANTIC, other.id, ARCHIVED)  # logged (Fix G)
            merged += 1
        survivor.supersedes, survivor.sources = sup, srcs
        survivor.confidence = min(0.95, survivor.confidence + 0.05 * len(to_merge))
        store.put_semantic(user_id, survivor)  # logged
    return {"merged": merged, "groups": ngroups}


def compact_all(model, root: str, *, tenants=None, verify: bool = True) -> list[tuple]:
    """Run compact_user for every (tenant, user) under `root`."""
    from .consolidate import discover_tenants
    out: list[tuple] = []
    for tenant in (tenants if tenants is not None else discover_tenants(root)):
        store = MemoryStore.for_tenant(tenant, root=root)
        for user_id in store.list_user_ids():
            out.append((tenant, user_id, compact_user(model, store, user_id, verify=verify)))
    return out
