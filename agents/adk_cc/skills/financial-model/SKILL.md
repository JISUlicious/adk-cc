---
name: financial-model
description: >
  Build a linked three-statement model (P&L, balance sheet, cash flow) from
  stated drivers, with the accounting basis and currency established first and a
  balance check that must tie. Use for budgeting, fundraising models, or scenario
  planning.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["the balance sheet balances in every period and the check is shown", "cash flow ties to the change in the cash line", "every driver is listed with its source or marked as an assumption", "accounting basis, currency and fiscal year are stated, or explicitly marked NOT ESTABLISHED"]}
---

# Three-statement financial model

The model's credibility rests on two things: the statements tie, and every
number traces to a driver someone can argue with. Build it so a reader can
change one assumption and watch the consequences propagate.

## Context first — this varies by country and entity

Reporting and tax treatment differ by jurisdiction, entity form and year. **Do
not recall rules from memory.** Establish, or state as not established:

- **Jurisdiction** and reporting framework (IFRS, US GAAP, a local standard) —
  these differ on revenue recognition, leases, capitalisation.
- **Entity type and size** — what a small private company files differs from a
  listed one; some regimes have simplified reporting.
- **Currency** and presentation currency; **fiscal year end** (not always
  December).
- **Tax**: rates, loss carryforward and credits are jurisdiction- and
  year-specific. Model tax as a **stated input** the user supplies, or fetch the
  current rule with `web_fetch` and cite the source and date. Never state a rate
  from memory.

Open the output with a context line, e.g.
`Context — basis: IFRS (user-stated) · currency: EUR · FY end: 31 Mar · tax rate: NOT ESTABLISHED, modelled as input`.

This is a modelling aid, **not accounting, tax or legal advice** — a qualified
accountant in the relevant jurisdiction should review anything that will be
filed, audited, or shown to investors.

## Structure

1. **Drivers sheet** — every assumption in one place, each labelled
   `[user] [computed] [fetched: source, date] [ASSUMPTION]`. Nothing hardcoded
   inside a formula.
2. **P&L** — revenue built from volume × price (or cohort × retention), not a
   growth rate applied to a total; costs split fixed/variable.
3. **Balance sheet** — working capital from days-based drivers (DSO, DPO, DIO),
   debt schedule with interest computed from the balance.
4. **Cash flow** — indirect method: net income, non-cash addbacks, working
   capital movements, investing, financing.

## The checks that make it a model rather than a spreadsheet

```
assets - (liabilities + equity) == 0        for every period   ← must be exact
closing cash (CF) == cash line (BS)         for every period
sum of segments == total                    where applicable
```
Show the check row. A model whose balance check is not visible is asking to be
trusted rather than verified.

## Scenarios

Base / downside / upside as **driver sets**, never as separately-typed numbers.
Report what breaks first in the downside (covenant, runway, hiring plan) and
when. State runway in months from the cash line, not from a rule of thumb.

## Output

The model (spreadsheet or code that regenerates it), plus: the driver list with
provenance, the tie-out checks, the scenario summary, and a short "what would
change this materially" note. Where the workspace has real history (actuals,
transaction exports), calibrate drivers on it and say which periods you used.
