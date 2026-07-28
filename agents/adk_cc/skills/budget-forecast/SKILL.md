---
name: budget-forecast
description: >
  Build a driver-based budget or rolling forecast and explain variance against
  actuals — price/volume/mix decomposition, runway, and a re-forecast when
  reality diverges. Use for annual planning, monthly close commentary, or
  "why did we miss?"
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["variance is decomposed into drivers (price, volume, mix, timing), not just reported as a total", "actuals used are read from real files or systems in this turn and their period is stated", "runway and coverage figures are computed from the cash line, not from a rule of thumb", "forecast changes name what new information caused them"]}
---

# Budget and forecast

A budget nobody re-forecasts is a wish. The work that matters is the loop:
plan → compare → explain → adjust, with each variance attributed to something
someone can act on.

## Establish the context before the numbers

Ask, or mark as not established: **fiscal year end** (not December everywhere),
**currency** (and whether any entity reports in another), **entity and
consolidation** scope, **accounting basis** (cash vs accrual changes when things
land), and payroll structure — employer costs, mandatory contributions and
leave accrual differ by country and are usually the largest line. Do not assume
a percentage for any of these and do not state one from memory: take it as a
user-supplied input, or `web_fetch` a current source and cite it with a date.

`Context — FY end: NOT ESTABLISHED (assumed Dec) · currency: USD · basis: accrual · payroll on-costs: user-supplied`

## Build the budget from drivers

Never a flat percentage on last year. Revenue: volume × price, or pipeline ×
conversion, or cohort × retention — the one that matches how the business
actually earns. Costs: headcount plan (roles, start months, fully-loaded cost as
an input), variable costs tied to their driver, fixed costs listed individually.

Read the real history where it exists in the workspace:
```bash
ls **/*.csv **/*.xlsx 2>/dev/null | head          # actuals exports
```
and calibrate seasonality and conversion on it rather than assuming a curve.

## Variance analysis that is worth reading

Decompose, don't just subtract:

```
Revenue variance  -420k
  price            -120k   (realised price 3% below plan)
  volume           -260k   (units 6% below plan, concentrated in <segment>)
  mix               -40k
Timing?            yes — 180k of it slips into next period (invoices dated <x>)
```
Separate **timing** from **permanent**: a slipped invoice and a lost customer
look identical in a monthly total and mean opposite things.

## Re-forecast honestly

When you change the forecast, say what new information caused it. A forecast
revised without a reason is a number nobody can hold. Report runway from the
cash line and state the assumptions that would extend or shorten it.

## Output

```
# Budget / forecast — <period>
Context (FY, currency, basis, scope)
Drivers and their sources
Plan vs actual — decomposed variance, timing vs permanent
Re-forecast — what changed and why
Runway / coverage — computed, with the cash basis stated
Watch items — the drivers most likely to move next period
```
