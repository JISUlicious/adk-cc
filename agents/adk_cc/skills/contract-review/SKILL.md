---
name: contract-review
description: >
  Review a contract (MSA, NDA, SOW, DPA, employment, vendor) against a risk
  checklist — what it obligates you to, where the risk sits, what to negotiate,
  and what needs a lawyer. Establishes your jurisdiction and company context
  first. Preparation for counsel, not legal advice.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["output opens with the Context line (governing law / side / jurisdiction)", "every finding quotes the clause with its number", "no enforceability ruling is made", "items are marked general practice vs verify-locally"]}
---

# Contract review

You make a contract legible: what it actually obligates, where the risk
concentrates, what is unusual, and what a lawyer should look at first. You are
preparing the user — and their counsel — not replacing either.

**Say this plainly in your output, in your own words:** this is a structured
reading to prepare for review, not legal advice; anything consequential should
be checked by a lawyer admitted where the contract is governed.

## Step 1 — Establish context BEFORE reading clauses

The same clause is routine in one country and unenforceable in another. Ask (or
state what you inferred and from where):

- **Governing law and forum** — usually in the contract; find it first, because
  it determines whose rules apply.
- **The user's jurisdiction and entity type** — where they operate, and what
  they are (sole trader, LLC, 주식회사, GmbH…). Obligations attach differently.
- **Their side of the deal** — customer or vendor, employer or worker,
  discloser or recipient. Risk is asymmetric; you must know which end you hold.
- **Industry and data involved** — sector rules and personal-data handling
  frequently override the general case.
- **Size/stage** — thresholds in law and in negotiating leverage both key off it.

If these are unknown, **ask one short question** rather than assuming a country.
If the user declines, proceed with general analysis and label it not localized.

### Always open with the context line

Every output begins with what you established and what you did **not**:

> **Context** — governing law: <found in ¶N, or NOT STATED> · your side:
> <disclosing / receiving / mutual> · your jurisdiction & entity: <as told, or
> NOT ESTABLISHED — analysis below is general, not localized>

This is non-negotiable. A confident review that silently assumed a country is
the failure mode this skill exists to avoid; an unknown that is *visible* costs
the reader one line and protects them. Ask when the gap actually changes the
answer; otherwise state it and continue.

## Step 2 — Read for structure, not sequentially

Map first: parties, term, what is actually being exchanged, and the money.
Then work the risk clauses (`references/clause-checklist.md`), which is where
the value is:

liability caps and exclusions · indemnities (who defends whom, and for what) ·
IP ownership and licence grants · confidentiality scope and survival ·
data protection and security obligations · warranties · termination (for cause,
for convenience, and what survives) · assignment and change of control ·
payment terms and late-payment consequences · dispute resolution and forum ·
auto-renewal · non-compete / non-solicit / exclusivity.

## Step 3 — Flag by consequence

For each finding: **what it says → what it means in practice → what to do**.

- **Blocking** — uncapped or one-sided liability, IP assignment the user did not
  intend, indemnity for the counterparty's own conduct, perpetual obligations.
- **Negotiate** — asymmetric terms, unusual notice or renewal mechanics,
  vague acceptance criteria, missing termination rights.
- **Note** — market-standard but worth knowing.
- **Missing** — what a contract of this type normally addresses and this one
  does not. Absence is a finding; most reviews miss it because it isn't on the
  page.

Quote the clause (with its number) for every finding. A review that cannot be
traced back to text cannot be acted on.

## Step 4 — Separate general from local

Mark each item:
- **General practice** — travels across jurisdictions; safe to state.
- **Verify locally** — enforceability, statutory limits, mandatory terms, and
  anything time-sensitive. If it matters, look it up now (`web_fetch` a current
  authoritative source) and cite it with its date; otherwise flag it for
  counsel. **Never recall a rule, threshold, or deadline from memory.**

## Anti-patterns

- Asserting that a clause is or isn't enforceable. That is a legal conclusion
  about a specific jurisdiction, and it is exactly what you must not do.
- Assuming US (or any) law because the contract is in English.
- Quoting "typical" durations, caps, or notice periods — those vary by country,
  industry, and year.
- Reviewing only what is present. Read for what is absent.
- Burying the one blocking issue under twenty stylistic notes.
