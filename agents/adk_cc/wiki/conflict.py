"""Conflict classification + resolution policy (pure, no IO, no LLM).

This is the heart of the "what happens when a user's inbox claim conflicts
with the shared domain wiki?" question. It is deliberately a PURE decision
function — like `permissions/engine.py::decide` — so every branch is unit
testable without a filesystem or a model. The librarian feeds it verdicts
(from an injectable classifier) and applies the resolutions it returns.

Two moments of conflict, two stances:
  - READ time (handled in search.py): a user's inbox shadows domain FOR
    THAT USER, but the discrepancy is surfaced, never silently preferred.
  - MERGE time (here): domain is the default authority; the burden of proof
    is on the inbox claim; the default is to QUARANTINE, never to silently
    overwrite domain.

Classify before resolving. Six classifications of an inbox claim vs the
current domain fact:
  NOVEL          domain has no such fact      → ADD
  AGREES         consistent with domain       → CORROBORATE (reinforce)
  REFINEMENT     narrows/qualifies a fact     → REFINE (qualified sub-fact)
  SUPERSESSION   newer value, time-ordered    → SUPERSEDE (append validity)
  CONTRADICTION  conflicts, no time order     → OVERTURN if corroborated by
                                                ≥N independent users, else
                                                CONTEST (record both, queue)
  ERROR          contradicts well-cited fact,
                 unsupported                  → REJECT (note; page untouched)

Three resolution tiers, encoded below:
  1. auto-resolve   NOVEL/AGREES/REFINEMENT/SUPERSESSION
  2. corroboration  CONTRADICTION overturns domain only with ≥N users
  3. adjudication   otherwise CONTEST/QUARANTINE → human review queue

Defenses:
  - cite-or-quarantine: an OVERTURN (overriding established domain via
    contradiction) requires an EXTERNAL source; without one it downgrades
    to QUARANTINE. Additive actions ride the inbox doc's own provenance.
  - sticky resolutions: a human adjudication for a claim-hash wins and is
    idempotent (no oscillation across runs).
  - auto-supersede is the configured default for time-ordered claims (the
    operator chose this over always-quarantine).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from .page import Page

# ---- classifications (verdict.classification) ----
NOVEL = "novel"
AGREES = "agrees"
REFINEMENT = "refinement"
SUPERSESSION = "supersession"
CONTRADICTION = "contradiction"
ERROR = "error"
CLASSIFICATIONS = frozenset(
    {NOVEL, AGREES, REFINEMENT, SUPERSESSION, CONTRADICTION, ERROR}
)

# ---- resolution actions (resolution.action) ----
ADD = "add"
CORROBORATE = "corroborate"
REFINE = "refine"
SUPERSEDE = "supersede"
OVERTURN = "overturn"
CONTEST = "contest"
QUARANTINE = "quarantine"
REJECT = "reject"

# Actions that publish into domain (used by the librarian to decide whether
# to archive the inbox doc vs leave it pending for a human).
PUBLISHING_ACTIONS = frozenset({ADD, CORROBORATE, REFINE, SUPERSEDE, OVERTURN, CONTEST})
# Actions that hold a claim back for human adjudication.
HELD_ACTIONS = frozenset({QUARANTINE, REJECT})


@dataclass
class ClaimRecord:
    """One inbox claim entering the merge, normalized for policy."""

    slug: str
    text: str
    user_id: str
    doc_id: str
    sources: list[str] = field(default_factory=list)  # EXTERNAL source ids
    created: str = ""
    type: str = "concept"                              # llm-wiki page category
    tags: list[str] = field(default_factory=list)      # ≤3 kebab labels

    @property
    def claim_hash(self) -> str:
        return claim_hash(self.slug, self.text)

    @property
    def has_external_source(self) -> bool:
        return bool(self.sources)


@dataclass
class Verdict:
    classification: str
    reason: str = ""
    confidence: float = 1.0


@dataclass
class Resolution:
    action: str
    claim: ClaimRecord
    reason: str = ""
    # for SUPERSEDE/OVERTURN: the prior domain value being replaced (provenance)
    replaces: Optional[str] = None


def claim_hash(slug: str, text: str) -> str:
    """Stable key for sticky resolutions: slug + normalized claim text.
    Whitespace-collapsed, lowercased — so trivial reformatting doesn't make
    a re-adjudicated claim look new."""
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(f"{slug}\x00{norm}".encode("utf-8")).hexdigest()[:16]


def resolve(
    verdict: Verdict,
    claim: ClaimRecord,
    *,
    support_count: int = 1,
    corroboration_n: int = 2,
    human_override: Optional[str] = None,
    domain_value: Optional[str] = None,
) -> Resolution:
    """Map a verdict to a resolution. PURE — no IO.

    `support_count`  distinct users asserting this claim's direction this run.
    `corroboration_n` threshold to overturn a domain fact (admin-tunable).
    `human_override`  a sticky human adjudication: "accept" | "reject" | None.
    `domain_value`    short excerpt of the domain fact being replaced (for the
                      provenance footer on SUPERSEDE/OVERTURN).
    """
    # Tier 3 short-circuit: a human decision wins and is idempotent.
    if human_override == "reject":
        return Resolution(REJECT, claim, reason="human-adjudicated: rejected")
    if human_override == "accept":
        # honor the human's accept; pick the natural publishing action
        action = SUPERSEDE if verdict.classification == SUPERSESSION else ADD
        return Resolution(
            action, claim, reason="human-adjudicated: accepted", replaces=domain_value
        )

    c = verdict.classification

    # Tier 1: auto-resolve.
    if c == NOVEL:
        return Resolution(ADD, claim, reason=verdict.reason or "new fact")
    if c == AGREES:
        return Resolution(CORROBORATE, claim, reason=verdict.reason or "corroborates")
    if c == REFINEMENT:
        return Resolution(REFINE, claim, reason=verdict.reason or "narrows scope")
    if c == SUPERSESSION:
        # operator chose auto-supersede for time-ordered updates.
        return Resolution(
            SUPERSEDE, claim, reason=verdict.reason or "supersedes (time-ordered)",
            replaces=domain_value,
        )
    if c == ERROR:
        return Resolution(REJECT, claim, reason=verdict.reason or "contradicts cited fact")

    # Tier 2: contradiction — corroboration can overturn, else adjudicate.
    if c == CONTRADICTION:
        if support_count >= corroboration_n:
            # cite-or-quarantine: overturning established domain needs an
            # EXTERNAL source, not just the asserting user's provenance.
            if not claim.has_external_source:
                return Resolution(
                    QUARANTINE, claim,
                    reason=(
                        f"contradiction corroborated by {support_count} users but "
                        "uncited — overturning domain needs a source"
                    ),
                )
            return Resolution(
                OVERTURN, claim,
                reason=f"overturned by {support_count}≥{corroboration_n} users + source",
                replaces=domain_value,
            )
        return Resolution(
            CONTEST, claim,
            reason=(
                f"true contradiction; only {support_count}<{corroboration_n} "
                "corroborating — record both, queue for review"
            ),
        )

    # Unknown classification → safest is to hold for a human.
    return Resolution(QUARANTINE, claim, reason=f"unclassified verdict: {c!r}")


# --------------------------------------------------------------------------
# deterministic fallback classifier (no LLM) — also a clean test seam.
# --------------------------------------------------------------------------
_NUM_RE = re.compile(r"\d[\d,]*\.?\d*")
_NEG_RE = re.compile(r"\b(not|no longer|isn't|aren't|never|incorrect|wrong)\b", re.I)


def heuristic_classify(claim: ClaimRecord, domain_page: Optional[Page]) -> Verdict:
    """A crude, deterministic classifier used when no model is available and
    as a stable default in tests. The LLM classifier supersedes it in
    production — but this keeps the merge pipeline runnable without a model.

    Rules (intentionally conservative):
      - no domain page                                  → NOVEL
      - claim text already substantially in the page    → AGREES
      - claim cites a newer date/version than the page  → SUPERSESSION
      - claim carries a negation or a different number   → CONTRADICTION
      - claim adds a qualifier the page lacks            → REFINEMENT
      - otherwise                                        → AGREES
    """
    if domain_page is None:
        return Verdict(NOVEL, "no existing domain page")
    body = domain_page.body.lower()
    text = claim.text.strip().lower()
    if text and text in body:
        return Verdict(AGREES, "claim already present")

    claim_nums = set(_NUM_RE.findall(claim.text))
    domain_nums = set(_NUM_RE.findall(domain_page.body))
    if _NEG_RE.search(claim.text):
        return Verdict(CONTRADICTION, "negation vs domain")
    if claim_nums and domain_nums and not (claim_nums & domain_nums):
        # different numeric value for the same topic — could be supersession
        # or contradiction; without a date signal, treat as contradiction.
        if re.search(r"\b(now|updated|as of|since|new)\b", text):
            return Verdict(SUPERSESSION, "updated numeric value")
        return Verdict(CONTRADICTION, "differing numeric value")
    if len(text) > len(body) * 0.3 and any(
        w in text for w in ("when", "if", "only", "except", "unless", "for")
    ):
        return Verdict(REFINEMENT, "adds a qualifier")
    return Verdict(AGREES, "no conflict detected")
