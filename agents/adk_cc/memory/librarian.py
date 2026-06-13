"""The offline librarian: the SINGLE writer that merges user inboxes into
the shared domain wiki.

Single-writer is the load-bearing invariant — it is what keeps concurrent
captures from producing the semantic conflicts Karpathy warns a textual
merge can't resolve. The librarian runs out-of-band (cron, see
`scripts/wiki_librarian.py`), never inside a user turn.

Pipeline (per tenant):

  collect   gather inbox docs across all users → ClaimRecords
            (skip no_promote/sensitive; skip already-queued unless a human
             has since adjudicated — sticky idempotency)
  cluster   group claims by target slug
  classify  injectable Classifier (LLM in prod, heuristic fallback / fake
            in tests) → Verdict per claim vs the current domain page
  resolve   conflict.resolve(...) — pure policy: auto-resolve / corroborate
            / adjudicate, cite-or-quarantine, sticky human overrides
  synthesize deterministic page assembly (provenance, validity windows,
            contested markers) — no LLM, so the bookkeeping is exact
  publish   atomic per-page write + changelog
  lint      rebuild index.md from the published pages
  archive   move published claims' inbox docs → merged/ (user keeps a copy);
            held claims (quarantine/reject) stay in inbox + go to the review
            queue with an auto sticky so re-runs don't pile up duplicates

The classification step is the only place a model is needed; everything
else is deterministic and unit-tested. `MergeReport` summarizes the run.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Union

from google.adk.models.llm_request import LlmRequest
from google.adk.utils.context_utils import Aclosing
from google.genai import types

from . import conflict
from .conflict import ClaimRecord, Resolution, Verdict
from .page import Page
from .store import InboxDoc, WikiStore

_log = logging.getLogger(__name__)

# A classifier: (claim, current domain page or None) -> Verdict. May be sync
# (the heuristic default) or async (the LLM classifier) — the librarian awaits
# the result when it's awaitable.
Classifier = Callable[
    [ClaimRecord, Optional[Page]], Union[Verdict, Awaitable[Verdict]]
]


@dataclass
class MergeReport:
    tenant_id: str
    claims_seen: int = 0
    skipped_no_promote: int = 0
    skipped_queued: int = 0
    actions: dict[str, int] = field(default_factory=dict)
    slugs_touched: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def bump(self, action: str) -> None:
        self.actions[action] = self.actions.get(action, 0) + 1


class Librarian:
    def __init__(
        self,
        store: WikiStore,
        *,
        classifier: Optional[Classifier] = None,
    ) -> None:
        self.store = store.ensure()
        # Default to the deterministic heuristic so the pipeline runs without
        # a model; production injects an LlmClassifier.
        self.classify: Classifier = classifier or conflict.heuristic_classify

    # -------------------------------------------------------------- run ----
    async def run(self) -> MergeReport:
        report = MergeReport(tenant_id=self.store.tenant_id)
        clusters = self._collect(report)
        for slug, claims in sorted(clusters.items()):
            try:
                await self._merge_slug(slug, claims, report)
            except Exception as e:  # noqa: BLE001 — one bad slug must not abort the run
                _log.warning("librarian: slug %s failed (%s: %s)", slug, type(e).__name__, e)
                report.errors.append(f"{slug}: {e}")
        self._lint()
        self.store.append_changelog(
            {"op": "merge_run", "actions": report.actions,
             "slugs": report.slugs_touched, "quarantined": len(report.quarantined)}
        )
        return report

    # ---------------------------------------------------------- collect ----
    def _collect(self, report: MergeReport) -> dict[str, list[tuple[InboxDoc, ClaimRecord]]]:
        clusters: dict[str, list[tuple[InboxDoc, ClaimRecord]]] = {}
        for user_id in self.store.list_user_ids():
            for doc in self.store.list_inbox(user_id):
                if doc.page.no_promote:
                    report.skipped_no_promote += 1
                    continue
                claim = ClaimRecord(
                    slug=doc.slug,
                    text=doc.page.body.strip(),
                    user_id=user_id,
                    doc_id=doc.doc_id,
                    sources=doc.page.sources,
                    created=str(doc.page.frontmatter.get("created", "")),
                )
                report.claims_seen += 1
                # sticky idempotency: if already queued and NOT since
                # human-resolved, leave it pending and don't reprocess.
                if (
                    self.store.is_quarantined(claim.claim_hash)
                    and self.store.human_override(claim.claim_hash) is None
                ):
                    report.skipped_queued += 1
                    continue
                clusters.setdefault(doc.slug, []).append((doc, claim))
        return clusters

    # ----------------------------------------------------------- merge ----
    async def _merge_slug(
        self, slug: str, items: list[tuple[InboxDoc, ClaimRecord]], report: MergeReport
    ) -> None:
        domain_page = self.store.read_domain_page(slug)
        domain_value = (domain_page.body.strip()[:160] if domain_page else None)

        # Verdicts first, so we can count the corroboration cohort for this slug.
        verdicts: list[Verdict] = []
        for (_doc, claim) in items:
            v = self.classify(claim, domain_page)
            if inspect.isawaitable(v):
                v = await v
            verdicts.append(v)
        overturn_users = {
            claim.user_id
            for (_doc, claim), v in zip(items, verdicts)
            if v.classification in (conflict.CONTRADICTION, conflict.SUPERSESSION)
        }
        support_count = len(overturn_users)
        n = self.store.corroboration_n

        page = domain_page or Page(slug=slug, frontmatter={"title": _titleize(slug)}, body="")
        published_docs: list[tuple[str, str]] = []  # (user_id, doc_id)
        touched = False

        for (doc, claim), verdict in zip(items, verdicts):
            res = conflict.resolve(
                verdict, claim,
                support_count=support_count,
                corroboration_n=n,
                human_override=self.store.human_override(claim.claim_hash),
                domain_value=domain_value,
            )
            report.bump(res.action)
            if res.action in conflict.HELD_ACTIONS:
                self._hold(res, verdict, report)
                continue
            # publishing action → fold into the page
            page = _apply_resolution(page, res)
            touched = True
            published_docs.append((claim.user_id, claim.doc_id))
            # a contested entry is queryable on the page AND queued for a human
            # to adjudicate (the page shows "contested" in the meantime).
            if res.action == conflict.CONTEST:
                self._enqueue_review(res, verdict, report)
            # record an auto sticky so re-runs are idempotent on this claim
            self.store.set_sticky(
                claim.claim_hash, action=res.action, by="auto", note=res.reason
            )

        if touched:
            self.store.write_domain_page(page)
            report.slugs_touched.append(slug)
            # archive only AFTER a successful page write (user keeps the copy)
            for user_id, doc_id in published_docs:
                self.store.archive_inbox(user_id, doc_id)

    def _enqueue_review(self, res: Resolution, verdict: Verdict, report: MergeReport) -> None:
        """Add a human-review-queue note for a claim (idempotent by hash)."""
        ch = res.claim.claim_hash
        if not self.store.is_quarantined(ch):
            self.store.add_quarantine(ch, {
                "slug": res.claim.slug,
                "user_id": res.claim.user_id,
                "doc_id": res.claim.doc_id,
                "claim": res.claim.text[:500],
                "classification": verdict.classification,
                "action": res.action,
                "reason": res.reason,
            })
            report.quarantined.append(ch)

    def _hold(self, res: Resolution, verdict: Verdict, report: MergeReport) -> None:
        """A QUARANTINE/REJECT claim: queue for human review, record an auto
        sticky, and LEAVE the inbox doc pending (the user/admin revisits)."""
        self._enqueue_review(res, verdict, report)
        self.store.set_sticky(
            res.claim.claim_hash, action=res.action, by="auto", note=res.reason
        )

    # ------------------------------------------------------------ lint ----
    def _lint(self) -> None:
        """Rebuild index.md from the published pages (deterministic). Keeps
        the wiki's navigational hand in sync without an LLM."""
        slugs = self.store.list_domain_pages()
        lines = ["# Index", ""]
        for slug in slugs:
            page = self.store.read_domain_page(slug)
            title = page.title if page else _titleize(slug)
            mark = " ⚠️ contested" if (page and page.contested) else ""
            lines.append(f"- [[{slug}]] — {title}{mark}")
        if not slugs:
            lines.append("_(empty — no pages yet)_")
        self.store.write_index("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# deterministic page synthesis (no LLM — exact, auditable bookkeeping)
# --------------------------------------------------------------------------
def _apply_resolution(page: Page, res: Resolution) -> Page:
    """Fold one publishing resolution into the page. Pure-ish (returns the
    mutated page). Provenance is always recorded; supersession appends a
    validity window; contradiction marks the page contested."""
    claim = res.claim
    fm = dict(page.frontmatter)
    fm.setdefault("title", _titleize(page.slug))
    # union external sources
    srcs = list(fm.get("sources") or [])
    for s in claim.sources:
        if s not in srcs:
            srcs.append(s)
    if srcs:
        fm["sources"] = srcs

    prov = _provenance(claim)
    body = page.body.rstrip()

    if res.action == conflict.ADD:
        body = _append_fact(body, f"{claim.text} {prov}")
    elif res.action == conflict.REFINE:
        body = _append_fact(body, f"(refinement) {claim.text} {prov}")
    elif res.action == conflict.CORROBORATE:
        # carry the corroborating claim's CONTENT, not just "corroborated by
        # X" — a claim that agrees on topic may still add specifics the page
        # lacks; dropping them would silently lose information.
        body = _append_section(
            body, "Corroborations",
            f"{claim.text} — independently corroborated by {claim.user_id}. {prov}",
        )
    elif res.action == conflict.SUPERSEDE:
        fm.setdefault("validity", [])
        fm["validity"].append({
            "value": claim.text[:200], "from": claim.created or "unknown",
            "supersedes": res.replaces, "source": claim.doc_id,
        })
        body = _append_fact(body, f"(current, supersedes prior) {claim.text} {prov}")
    elif res.action == conflict.OVERTURN:
        fm["contested"] = False  # corroboration resolved it
        body = _append_fact(
            body, f"(overturned prior, corroborated) {claim.text} {prov}"
        )
    elif res.action == conflict.CONTEST:
        fm["contested"] = True
        body = _append_section(
            body, "Contested",
            f"Conflicting claim from {claim.user_id}: {claim.text} {prov}\n"
            f"  (shared wiki currently states otherwise — unresolved)",
        )
    page.frontmatter = fm
    page.body = body.rstrip() + "\n"
    return page


def _provenance(claim: ClaimRecord) -> str:
    bits = [f"by {claim.user_id}", f"doc {claim.doc_id}"]
    if claim.created:
        bits.append(claim.created)
    if claim.sources:
        bits.append("sources: " + ", ".join(claim.sources))
    return "_(" + "; ".join(bits) + ")_"


def _append_fact(body: str, text: str) -> str:
    return _append_section(body, "Facts", text)


def _append_section(body: str, section: str, bullet: str) -> str:
    """Append `- bullet` under a `## section` heading, creating it if absent."""
    heading = f"## {section}"
    line = f"- {bullet}"
    if heading in body:
        # insert after the heading block — append at end of that section.
        lines = body.splitlines()
        out: list[str] = []
        inserted = False
        for i, ln in enumerate(lines):
            out.append(ln)
            if ln.strip() == heading and not inserted:
                # find end of this section (next heading or EOF) then insert
                j = i + 1
                while j < len(lines) and not lines[j].startswith("## "):
                    out.append(lines[j])
                    j += 1
                out.append(line)
                out.extend(lines[j:])
                inserted = True
                break
        return "\n".join(out)
    sep = "\n\n" if body.strip() else ""
    return f"{body.rstrip()}{sep}{heading}\n{line}"


def _titleize(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-")) if slug else slug


# --------------------------------------------------------------------------
# LLM-backed classifier (production). Tests inject fakes; the heuristic is
# the no-model default.
# --------------------------------------------------------------------------
_CLASSIFY_PROMPT = (
    "You maintain a shared knowledge wiki. Classify how a NEW user claim "
    "relates to the CURRENT shared page on the same topic.\n\n"
    "Reply with EXACTLY one line:\n"
    "CLASS: <one of: novel | agrees | refinement | supersession | "
    "contradiction | error> | <short reason>\n\n"
    "Definitions:\n"
    "- novel: the page has no such fact yet.\n"
    "- agrees: consistent with the page (reinforces it).\n"
    "- refinement: narrows/qualifies an existing fact (adds a condition).\n"
    "- supersession: a newer value that replaces an older one over time.\n"
    "- contradiction: conflicts with the page, with no clear time ordering.\n"
    "- error: the claim contradicts a well-established page fact and looks wrong.\n\n"
    "CURRENT PAGE (may be empty):\n{page}\n\n"
    "NEW CLAIM:\n{claim}\n"
)


class LlmClassifier:
    """Model-backed classifier. `model` is any ADK BaseLlm-ish object with
    `generate_content_async(LlmRequest, stream=False)`. Each call is bounded
    by `timeout_s` and falls back to the heuristic verdict on timeout, parse
    failure, or transport error (never raises) — so one stuck/looping model
    call can't stall the whole merge."""

    def __init__(self, model, *, timeout_s: float = 45.0) -> None:
        self._model = model
        self._timeout_s = timeout_s

    async def aclassify(self, claim: ClaimRecord, domain_page: Optional[Page]) -> Verdict:
        try:
            raw = await asyncio.wait_for(
                self._generate(claim, domain_page), timeout=self._timeout_s
            )
            return _parse_verdict(raw) or conflict.heuristic_classify(claim, domain_page)
        except asyncio.TimeoutError:
            _log.warning(
                "librarian: LLM classify timed out (%.0fs) for slug %s — heuristic",
                self._timeout_s, claim.slug,
            )
            return conflict.heuristic_classify(claim, domain_page)
        except Exception as e:  # noqa: BLE001
            _log.warning("librarian: LLM classify failed (%s: %s)", type(e).__name__, e)
            return conflict.heuristic_classify(claim, domain_page)

    async def _generate(self, claim: ClaimRecord, domain_page: Optional[Page]) -> str:
        page_text = domain_page.body.strip() if domain_page else "(no page yet)"
        prompt = _CLASSIFY_PROMPT.format(page=page_text[:3000], claim=claim.text[:1500])
        req = LlmRequest(
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(),
        )
        raw = ""
        async with Aclosing(self._model.generate_content_async(req, stream=False)) as agen:
            async for resp in agen:
                content = getattr(resp, "content", None)
                for p in (getattr(content, "parts", None) or []):
                    if not getattr(p, "thought", None) and getattr(p, "text", None):
                        raw += p.text
        return raw


def _parse_verdict(raw: str) -> Optional[Verdict]:
    for line in (raw or "").splitlines():
        s = line.strip()
        if s.upper().startswith("CLASS:"):
            rest = s[len("CLASS:"):].strip()
            cls, _, reason = rest.partition("|")
            cls = cls.strip().lower()
            if cls in conflict.CLASSIFICATIONS:
                return Verdict(cls, reason.strip())
    return None
