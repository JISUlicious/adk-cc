"""PROTOTYPE: topic canonicalization for memory consolidation.

Problem (see the long-run investigation): consolidate_user clusters episodics by
EXACT topic slug, and the capture LLM emits a fresh slug for the same fact every
turn ("architecture-focus", "cpu-architecture-focus", "design-approach", ...),
so nothing clusters and semantic ≈ episodic. This module maps near-duplicate
topics onto a shared canonical slug so consolidation actually folds them.

Two canonicalizers, mirroring consolidation's own deterministic-default /
optional-LLM split:
  - deterministic_canonical: token-set clustering (union-find over Jaccard +
    subset). Zero model calls. Catches prefix/superset/word-order variants
    (cpu-architecture-focus ≡ architecture-focus, memory-bandwidth ⊂
    memory-bandwidth-workloads). Misses pure synonyms (design-approach vs
    architecture-focus).
  - make_llm_canonical: one model call groups topics by meaning. Catches the
    synonyms the deterministic pass can't.

Both return {original_topic: canonical_topic}. NOT yet wired into
consolidate_user — pass the mapping by rewriting episodic topics, or inject
later as an optional `topic_canonicalizer` parameter.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

# Generic / scoping tokens that shouldn't drive a topic's identity.
_STOP = {
    "cpu", "core", "cores", "project", "private", "my", "the", "a", "an",
    "of", "for", "to", "is", "in", "on", "s", "user", "users", "context",
    "design", "focus", "preference", "preferences", "habit", "target",
}

# Canonical maps topic -> canonical slug
CanonicalMap = dict


def _tokens(slug: str) -> frozenset[str]:
    return frozenset(
        t for t in slug.replace("_", "-").split("-") if t and t not in _STOP
    )


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def deterministic_canonical(topics: list[str], *, threshold: float = 0.5) -> CanonicalMap:
    """Cluster topics by token overlap (Jaccard >= threshold OR subset), then
    map every member to one representative (fewest meaningful tokens = most
    general, ties broken lexicographically). No model calls."""
    uniq = list(dict.fromkeys(topics))
    toks = {t: _tokens(t) for t in uniq}
    parent = {t: t for t in uniq}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(uniq)):
        for j in range(i + 1, len(uniq)):
            a, b = uniq[i], uniq[j]
            ta, tb = toks[a], toks[b]
            if not ta or not tb:
                continue
            if ta <= tb or tb <= ta or _jaccard(ta, tb) >= threshold:
                union(a, b)

    clusters: dict[str, list[str]] = {}
    for t in uniq:
        clusters.setdefault(find(t), []).append(t)

    mapping: CanonicalMap = {}
    for members in clusters.values():
        rep = sorted(members, key=lambda m: (len(toks[m]) or 99, m))[0]
        for m in members:
            mapping[m] = rep
    return mapping


_CANON_PROMPT = (
    "You deduplicate memory topics. Several slugs below may name the SAME "
    "underlying fact about the user or their project. Group those that mean the "
    "same thing; leave genuinely distinct ones in their own group.\n\n"
    "Output ONE line per group, EXACTLY:\n"
    "<canonical-kebab-slug>: member1, member2, ...\n"
    "Use a short, descriptive canonical slug (it may equal a member). Cover "
    "every topic exactly once.\n\nTOPICS:\n{topics}"
)


def make_llm_canonical(model) -> Callable[..., CanonicalMap]:
    """Return a canonicalizer that uses one model call to group topics by
    meaning. Falls back to deterministic clustering for anything the model
    leaves uncovered (or on any failure)."""
    from google.adk.models.llm_request import LlmRequest
    from google.adk.utils.context_utils import Aclosing
    from google.genai import types

    async def _ask(topics: list[str]) -> str:
        listing = "\n".join(f"- {t}" for t in topics)
        req = LlmRequest(
            contents=[types.Content(role="user",
                                    parts=[types.Part(text=_CANON_PROMPT.format(topics=listing))])],
            config=types.GenerateContentConfig(),
        )
        out = ""
        async with Aclosing(model.generate_content_async(req, stream=False)) as agen:
            async for resp in agen:
                for p in (getattr(getattr(resp, "content", None), "parts", None) or []):
                    if not getattr(p, "thought", None) and getattr(p, "text", None):
                        out += p.text
        return out

    def canon(topics: list[str], facts: Optional[list[str]] = None) -> CanonicalMap:
        uniq = list(dict.fromkeys(topics))
        mapping: CanonicalMap = {}
        try:
            raw = asyncio.run(_ask(uniq))
            for line in raw.splitlines():
                s = line.strip().lstrip("-* ").strip()
                if ":" not in s:
                    continue
                canonical, _, rest = s.partition(":")
                canonical = canonical.strip()
                for member in rest.split(","):
                    m = member.strip()
                    if m in uniq:
                        mapping[m] = canonical or m
        except Exception:  # noqa: BLE001 — fall back wholesale
            mapping = {}
        # backfill anything the model dropped with the deterministic clusters
        if len(mapping) < len(uniq):
            det = deterministic_canonical(uniq)
            for t in uniq:
                mapping.setdefault(t, det.get(t, t))
        return mapping

    return canon
