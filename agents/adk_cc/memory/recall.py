"""Budgeted recall over a user's memory — the always-injected surface.

Memory is autonomous: every turn a small, token-budgeted block of the user's
relevant memories is injected into the model (the Hermes "tiny always-on
context" pattern), so the agent reuses what it knows without being asked.
Semantic (consolidated, durable) facts rank ahead of episodic (raw) ones.
"""

from __future__ import annotations

from typing import Optional

from .store import MemoryStore, SEMANTIC

_CHARS_PER_TOKEN = 4


def recall_context(
    store: MemoryStore,
    user_id: str,
    query: str,
    *,
    budget_tokens: int = 600,
) -> str:
    """Assemble a budgeted memory block for injection, or "" if nothing
    relevant. Semantic facts first (with a confidence hint), then episodic."""
    if not query:
        return ""
    budget_chars = max(0, budget_tokens) * _CHARS_PER_TOKEN
    hits = store.search(user_id, query, limit=8)
    if not hits:
        return ""

    sem_lines: list[str] = []
    epi_lines: list[str] = []
    for h in hits:
        tier = SEMANTIC if h.collection.endswith("/" + SEMANTIC) else "episodic"
        if tier == SEMANTIC:
            conf = h.frontmatter.get("confidence")
            tag = f" (confidence {conf})" if conf is not None else ""
            sem_lines.append(f"- {h.snippet}{tag}")
        else:
            epi_lines.append(f"- {h.snippet}")

    parts: list[str] = ["# Memory (recalled for this turn)"]
    if sem_lines:
        parts.append("## Known facts")
        parts.extend(sem_lines)
    if epi_lines:
        parts.append("## Recent notes")
        parts.extend(epi_lines)

    block = "\n".join(parts)
    if len(block) > budget_chars:
        block = block[:budget_chars].rstrip()
    return block
