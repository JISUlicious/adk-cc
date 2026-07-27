# Authoring conventions for built-in skills

Rules every skill in this directory follows. They exist because a built-in ships
to everyone: a skill that is subtly wrong for a user's country, company form, or
year is worse than no skill at all.

## 1. Context first, facts never

**Do not embed jurisdiction-, entity-, or time-bound facts in a skill.** No
statutory limits, tax rates, filing deadlines, notice periods, mandatory
clauses, threshold amounts, or "typical" terms. They differ by country, by
entity type, and by year — and a skill stating them will be confidently wrong
for most readers, with no signal that it is wrong.

Encode instead:

- **General knowledge of the field** — the methodology, the categories, the
  questions a practitioner asks, the shape of a good output, the failure modes.
  This travels: risk allocation, liability caps, IP assignment and termination
  are concepts everywhere, even though their rules are local.
- **A context-establishing step** — the skill's *first* action is to determine
  the user's actual situation, not to assume one.
- **Live verification** — when a specific rule genuinely matters, look it up at
  runtime (`web_fetch` a current authoritative source) and cite it with its
  date. Never recall it from training.

### The context to establish

Ask, or infer from the workspace and say what you inferred:

| Dimension | Why it changes the answer |
|---|---|
| Jurisdiction(s) | Governing law, enforceability, mandatory terms, filings |
| Entity type | Sole proprietor / LLC / GmbH / 주식회사 / etc. — duties differ |
| Company size & stage | Thresholds for obligations frequently key off headcount or revenue |
| Industry | Sector regulation often overrides the general case |
| Counterparty & relationship | Consumer vs. B2B, employee vs. contractor, domestic vs. cross-border |
| Language of record | Which language governs, and what the user needs the output in |

If the user has not said, **ask before producing jurisdiction-sensitive output**
— one short question beats a page of the wrong country's assumptions. If the
user declines to specify, produce the general-methodology version and label it
explicitly as not localized.

## 2. Say what is general and what is local

Separate them visibly in the output:

- **General practice** — applies broadly; safe to state.
- **Local/verify** — depends on jurisdiction or current rules; either verified
  live with a cited source and date, or flagged as needing confirmation.
- **Unknown** — say so. Do not fill gaps with plausible-sounding specifics.

## 3. Advice boundary (Tier C domains)

Legal, tax, employment, regulatory, and financial-advice skills draft,
structure, triage, and organize. They never issue a professional opinion.

Each such skill states plainly, in its own words, near the top of its output:
this is preparation, not advice from a qualified professional, and anything
consequential should be reviewed by one admitted in the relevant jurisdiction.
Not a disclaimer bolted on at the end — an honest framing of what the work is.

Never imply that following the skill's output discharges a legal, tax, or
regulatory obligation.

## 4. General style

- Trigger `description` states *when* to use the skill; that string is the only
  thing the model sees when choosing.
- Ground work in the user's real artifacts (repo, documents, data) rather than
  templates.
- Prefer a short skill with sharp judgment over a long one that lists everything.
- Put depth in `references/`; keep `SKILL.md` navigable.
- Name the anti-patterns. Knowing what not to do transfers better than another
  checklist.
