---
name: proposal-rfp
description: >
  Write a proposal or answer an RFP/RFI/security questionnaire — requirement
  matrix, compliant answers backed by evidence, and an explicit gap list instead
  of invented capabilities. Use for bids, vendor questionnaires, and SOW drafts.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["every capability claim maps to evidence in the workspace or a user statement, never to plausibility", "certifications, audits and compliance claims are only asserted if evidenced, otherwise listed as gaps", "every stated requirement has a response and the coverage count is reported", "commercial terms and dates are marked as needing approval rather than filled in"]}
---

# Proposal / RFP response

A bid is scored on compliance first and persuasion second. The two ways to lose
badly are missing a mandatory requirement and claiming something you cannot
evidence — the second is worse, because it can survive the bid and detonate in
the contract.

## Never invent these

Certifications (SOC 2, ISO 27001, HIPAA, PCI, national or sector schemes),
audit results, insurance cover, headcount, uptime figures, customer names,
reference logos, security controls, data-residency guarantees. If the workspace
or the user does not evidence it, it goes in the **gap list**, not the answer.
A fabricated certification claim is fraud in a procurement context.

## Context, then requirements

Ask, or mark not established: **buyer type and country** — public-sector
procurement is rule-bound and the rules differ by jurisdiction (mandatory
formats, deadlines, clarification channels, evaluation weightings); **contract
vehicle**; **submission deadline and format**; **evaluation criteria and
weightings** if published; **incumbent** if any. Procurement and contracting
rules are jurisdiction-specific and this is **not legal advice** — commercial and
contractual terms need a human owner and, where it matters, counsel. Never state
a procurement rule, deadline or threshold from memory; if one governs the bid,
`web_fetch` the issuing body's current text and cite it with a date.

### Build the requirement matrix first
Parse the document into a numbered list — every "shall", "must", "should",
"describe", and every question in an appendix. Then:

| # | Requirement | Type (M/S/O) | Response | Evidence | Owner | Status |

Report coverage explicitly: `74 requirements · 68 answered · 4 gaps · 2 need
approval`. An unanswered mandatory requirement is usually an automatic fail;
surface it early enough to decide whether to bid at all.

## Answering

- Answer **the question asked**, in their words, in their order. Evaluators score
  against a rubric; creative restructuring loses points.
- Lead each answer with the direct response, then the evidence, then the
  differentiator. Not the reverse.
- Evidence beats adjectives: a metric, an architecture detail, a named process,
  a reference (only with permission).
- Where you partially meet a requirement, say so and state the path — a credible
  partial usually scores better than an overclaim that fails diligence.

## Bid/no-bid, honestly

If the gap list contains mandatory items you cannot close, say so plainly before
the effort goes in. That judgement is worth more than a polished losing bid.

## Output

```
Requirement matrix (coverage counts)
Drafted responses
GAPS — requirement, why, options (partner / roadmap / decline)
[NEEDS APPROVAL] — pricing, dates, terms, references
Bid/no-bid read with the reasoning
```
