---
name: pricing-analysis
description: >
  Analyse or design pricing and packaging — value metric, tier structure,
  willingness-to-pay evidence, margin and elasticity math, and a migration plan
  for existing customers. Use for pricing changes, new packaging, or discount
  policy.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["the value metric is tested against real usage data where available, not asserted", "margin math is computed per tier including variable delivery cost", "the break-even volume change for the price move is stated", "the existing-customer migration path and its revenue effect are covered"]}
---

# Pricing and packaging analysis

Pricing is the highest-leverage number in a business and the least often
computed. This skill is about grounding it: what customers are buying, what it
costs to serve, and what happens to revenue when the price moves.

## Context that changes the answer

Ask, or mark not established: **market and currency** (list prices, purchasing
power and payment norms differ by country); **tax treatment** — whether prices
are quoted inclusive or exclusive of consumption tax differs by market and is a
presentation decision with revenue consequences; **customer type** (consumer,
SMB, enterprise, public sector — procurement rules differ); **contract length
and billing period**; **channel** (direct, marketplace, reseller — each takes a
cut you must model).

Do **not** state "typical" prices, discounts, margins or conversion rates for an
industry from memory. If a benchmark matters, fetch it with `web_fetch`, cite the
source and date, and treat it as context rather than a target. Local tax,
consumer-protection and price-display rules vary by jurisdiction; this is
analysis, **not legal or tax advice**.

## Work

### 1. Find the value metric
What grows with the value the customer gets — seats, usage, transactions,
outcomes? Test it against real data if the workspace has any:
```python
df.groupby("account")[["usage_metric", "revenue"]].sum().corr()
```
A value metric that does not track value creates the two classic failures:
customers who feel punished for adopting, and whales paying the same as minnows.

### 2. Cost to serve, per tier
Variable delivery cost (infra, support load, payment fees, channel share) per
tier and per segment. Gross margin by tier, not blended — blended margin hides
the tier that is losing money.

### 3. Willingness to pay — evidence, not intuition
Best to worst: realised win/loss and discount data you can read; structured
research (Van Westendorp, conjoint, gradient testing); competitor list prices
with dates; expert guess (label it as such). Report which one you used.

### 4. The math of the move
```
break-even volume change = -Δprice / (price + Δprice)      # to hold gross profit
elasticity implied by the plan            ← state it; is it plausible here?
revenue bridge: existing base, migration, new customers, churn effect
```
Say what the plan assumes about elasticity, because that is the assumption that
usually decides whether the change works.

### 5. Migration
Existing customers are the risk. Grandfathering, notice, price-lock length, and
the support load of the change. Model the revenue effect of each option.

## Output

Recommended structure with the value metric and its evidence; per-tier margin;
the break-even and elasticity math; the migration plan; and the two or three
signals to watch after launch that would say it is going wrong.
