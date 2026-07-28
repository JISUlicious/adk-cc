---
name: dcf-model
description: >
  Value a business or project by discounted cash flow — free cash flow build,
  a discount rate assembled from live-sourced inputs, terminal value by two
  methods, and a sensitivity grid. Use for valuation, investment cases, or
  buy/build decisions with multi-year cash effects.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["every discount-rate input is either user-supplied or fetched with a cited source and date, never recalled", "terminal value is computed two ways and the gap is reported", "the sensitivity grid spans discount rate and growth, and the implied terminal share of value is shown", "the valuation is stated as a range with the assumptions that drive the spread"]}
---

# DCF valuation

A DCF is an opinion with arithmetic attached. Most of the answer usually sits in
the terminal value and the discount rate — so those two get the scrutiny, and
the model reports how much of the result depends on them.

## Rates are facts about a time and place — fetch them, never recall them

The risk-free rate, equity risk premium, corporate tax rate, and country risk
premium all change with the country and the date. A remembered number is
confidently wrong. For each input:

- take it from the **user** if they have a house assumption (best), or
- **fetch it live** (`web_fetch`) and cite source + date, or
- mark it `[ASSUMPTION]` and put it in the sensitivity grid.

State the context up front:
`Context — currency: KRW · country: NOT ESTABLISHED (risk-free rate modelled as input) · valuation date: 2026-07-28 · basis: unlevered FCF`.

Also establish **entity and purpose**: an early-stage company, a mature private
business and a listed one call for different approaches, and a valuation for a
transaction, an impairment test or an internal decision have different standards.
This is analysis, **not investment, tax or legal advice**; a valuation used for a
transaction or a filing needs a qualified professional in that jurisdiction.

## Build

1. **Free cash flow** — unlevered: EBIT × (1 − tax) + D&A − capex − Δworking
   capital. Say explicitly whether cash flows and the discount rate are both
   nominal or both real, and both in the same currency.
2. **Forecast horizon** — long enough to reach a steady state; say why.
3. **Discount rate** — WACC from its parts, each with provenance; show the
   components rather than a single asserted number.
4. **Terminal value** — compute **both**: perpetuity growth and exit multiple.
   Report both and the gap. Perpetuity growth above long-run nominal GDP growth
   for that economy is a red flag; if you use one, justify it.
5. **Bridge to equity value** — enterprise value − net debt ± other claims;
   list what you included.

## Report what the answer depends on

```
terminal value as % of total EV        ← if >75%, say so plainly
sensitivity: WACC ±1.5pp  ×  g ±1.0pp  ← grid, not a point estimate
break-even: the growth rate that justifies today's asking price
```

## Output

Value as a **range** with the two or three assumptions that drive it, the
sensitivity grid, the FCF build, the input table with provenance and dates, and
a short note on what this method handles badly for this asset (option value,
cyclicality, pre-revenue businesses).
