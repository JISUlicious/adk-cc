---
name: nda-triage
description: >
  Triage an incoming NDA fast — classify it as routine, needs-negotiation, or
  escalate-to-counsel, with the specific clauses driving that call. Establishes
  jurisdiction and which side you are on first. Preparation for counsel, not
  legal advice.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["output opens with the Context line", "the verdict is GREEN/YELLOW/RED with the driving clauses quoted", "no enforceability ruling is made", "IP assignment and restraint clauses were explicitly checked for"]}
---

# NDA triage

NDAs arrive constantly and most are routine, so the job is **speed with a
reliable floor**: sort quickly, and never let an unusual one through because it
looked like the others.

**State plainly in your output:** this is a triage read to prepare for review,
not legal advice; anything you flag as escalate should go to a lawyer admitted
where the agreement is governed.

## Step 1 — Two facts before anything else

1. **Which side are you on?** Disclosing, receiving, or mutual. Every clause
   below is read in the opposite direction depending on the answer — a broad
   definition of Confidential Information protects a discloser and burdens a
   recipient.
2. **Governing law and the user's jurisdiction/entity.** Find the clause; if the
   contract is silent or the user's context is unknown, ask. Do not assume a
   country from the document's language.

Also worth knowing when available: industry, whether personal data is involved,
and whether this precedes a specific deal or is speculative.

### Always open with the context line

Every output begins with what you established and what you did **not**:

> **Context** — governing law: <found in ¶N, or NOT STATED> · your side:
> <disclosing / receiving / mutual> · your jurisdiction & entity: <as told, or
> NOT ESTABLISHED — analysis below is general, not localized>

This is non-negotiable. A confident review that silently assumed a country is
the failure mode this skill exists to avoid; an unknown that is *visible* costs
the reader one line and protects them. Ask when the gap actually changes the
answer; otherwise state it and continue.

## Step 2 — Classify

**GREEN — routine.** Mutual or appropriately one-sided for the user's position;
a bounded definition of confidential information; standard exclusions (already
known, independently developed, publicly available, lawfully received,
compelled disclosure); a term that ends; no IP transfer; no non-compete or
non-solicit; obligations limited to the stated purpose.

**YELLOW — negotiate.** One or two of: perpetual or unusually long
confidentiality; asymmetric obligations where mutual was expected; broad
"residuals" or feedback grants; missing standard exclusions; onerous return or
destruction duties; unclear or unbounded purpose.

**RED — escalate to counsel.** Any of: **IP assignment or licence** hidden in an
NDA; **non-compete, non-solicit, or exclusivity**; indemnities or liquidated
damages; a purpose broad enough to cover the user's whole business; obligations
binding affiliates the user cannot control; personal data handling with no
protection terms; or anything you genuinely cannot parse.

An NDA that quietly transfers IP or restricts future work is the single most
important thing this triage catches — that is the failure mode to be paranoid
about.

## Step 3 — Report

Verdict, then the clauses that drove it (quote them with numbers), then the
specific asks if YELLOW. Two paragraphs beats two pages; triage that takes as
long as a full review has failed at its job.

## Boundaries

- **Never rule on enforceability.** Whether a restriction holds up varies by
  jurisdiction and is a legal conclusion — flag it, refer it.
- **Never state "standard" durations or thresholds from memory.** Market
  practice differs by country, industry, and year. If a specific rule matters,
  verify it live (`web_fetch`, cite the source and date) or refer it.
- Classify by what the text says, not by who sent it.
- If the same counterparty's paper keeps arriving, say so — a reviewed template
  beats repeated triage.
