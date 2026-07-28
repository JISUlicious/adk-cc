---
name: performance-review
description: >
  Write an evidence-based performance review or calibration input — specific
  examples tied to expectations, actionable development points, and bias checks.
  Establishes the employment context first; never states local employment rules.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["the output opens with a context line naming jurisdiction, entity and what is NOT ESTABLISHED", "every rating is supported by dated, specific examples rather than adjectives", "no statutory process, notice, appeal right or termination step is stated from memory", "underperformance content routes to HR/counsel instead of prescribing a process"]}
---

# Performance review

A review is useful when the person can act on it and a stranger could see why
the rating is what it is. Adjectives fail both tests.

## Context first — and a hard boundary

Ask, or mark as not established: the **jurisdiction** where the person is
employed, the **entity** employing them (subsidiary, employer-of-record,
contractor — these carry different obligations), the review framework in use, the
period covered, and the expectations set at its start.

`Context — jurisdiction: NOT ESTABLISHED · entity: NOT ESTABLISHED · framework: user-supplied · period: Q1–Q2 2026`

**Performance management that could affect someone's employment is not a
drafting exercise.** Improvement plans, warnings, demotion, non-renewal and
termination are governed by local employment law and often by works councils,
collective agreements or contract terms — the required steps, timelines,
documentation and consultation differ by country and sometimes by region.
Never state those steps from memory. Where a rule matters, `web_fetch` a current
source and cite it with a date — and still route the decision to HR and counsel.
This is **not legal advice**.

If the request is heading toward discipline or exit, say so plainly and stop at
documenting observed facts. Documenting behaviour accurately is the useful and
safe contribution; prescribing a process is neither.

## Writing the review

### 1. Gather evidence before forming a view
Pull real artifacts from the period — shipped work, incidents handled, reviews
given, documents written. In a code workspace:
```bash
git log --author="<name>" --since="<period start>" --format='%ad %s' --date=short
```
Use artifacts as memory aids for what happened, never as a productivity metric —
commit counts measure nothing worth managing.

### 2. Structure each point as evidence → impact → expectation
> "In the March migration you found the compatibility break the design review
> missed (evidence), which avoided a rollback affecting all customers (impact).
> That is above the bar for this level, which asks for design participation
> rather than independent risk-finding (expectation)."

Ratings without dated examples are opinions. If you cannot produce the example,
you cannot support the rating — go and find it or change the rating.

### 3. Development points that can be acted on
Name the specific behaviour, the situation to practise it in, and what better
would look like. Two or three, prioritised. "Be more strategic" is not
feedback; "in design reviews, state the trade-off you rejected and why" is.

### 4. Bias checks before submitting
- **Recency** — does the evidence span the whole period, or just the last month?
- **Similarity** — are you rating closeness to how you work?
- **Attribution** — is team success credited, and are their constraints noted?
- **Language** — check descriptions of personality (warmth, assertiveness,
  "abrasive", "nice") that would not appear in a review of a peer doing the same
  work; describe behaviour and impact instead.
- **Comparability** — would this text justify the same rating for someone else?

## Output

```
Context line
Summary + rating (with the framework's own wording)
Strengths — evidence → impact → expectation
Development — behaviour, situation, what better looks like
Bias check notes
For HR/counsel: anything touching employment consequences
```
