---
name: competitive-analysis
description: >
  Map competitors from sourced evidence — capability and pricing comparison,
  positioning gaps, and where they are likely to move next, with every claim
  cited and dated. Use for positioning, win/loss review, or a market landscape.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["every factual claim about a competitor carries a source URL and a date it was checked", "unverifiable claims are labelled as inference rather than stated as fact", "the comparison axes are the ones buyers actually decide on, and that is justified", "the analysis says what would change the conclusion (a pricing move, a launch)"]}
---

# Competitive analysis

The failure mode here is not being wrong — it is being confidently stale. Vendor
pages, pricing and feature sets change constantly, and a model's recollection of
a competitor is a snapshot of an unknown date. **Every factual claim gets a
source and a check-date, or it is labelled as inference.**

## Work

### 1. Frame the comparison around the buyer's decision
Ask, or state: who is the buyer, what alternatives do they actually consider
(including "do nothing" and "build it internally"), and what do they decide on?
Compare on those axes. A feature grid comparing what *you* built is a mirror,
not an analysis. Note that the competitive set is often **regional** — the
alternatives a buyer in one country considers are frequently not the ones in
another; ask which market this is for.

### 2. Gather evidence, with dates
```
web_fetch each competitor's pricing, docs and changelog  → cite URL + date
```
Also mine what you already have: won/lost deal notes, support tickets mentioning
a competitor, and — if the workspace holds them — analyst notes or RFP responses.
Public signals worth reading: changelog cadence, job postings (what they are
building), docs depth, status page history.

### 3. Build the comparison
| Axis | Us | A | B | Source/date |
Mark each cell `[verified: url, date]`, `[inferred]`, or `[unknown]`. An honest
`[unknown]` is more useful than a plausible guess, because the reader can go get
it.

Then the parts a grid cannot show:
- **Positioning** — the sentence each competitor would use about itself, and
  who each is genuinely best for. Say where they beat you; an analysis where you
  win every row will not be believed and should not be.
- **Structural advantages** — distribution, data, switching costs, integrations.
  These decide more deals than features.
- **Direction** — what their hiring, changelog and pricing changes suggest is
  next, labelled as inference.

### 4. Conclusions that are actionable
Where you win and lose, by segment. The gaps worth closing and the ones worth
conceding. What would change the picture — a competitor's price move, a launch,
a platform shift — so the reader knows when to revisit this.

## Output

```
# Competitive landscape — <market/segment> · as of <date>
Buyer and alternatives considered
Comparison table (every cell sourced or labelled)
Positioning map — who each is genuinely best for
Structural advantages
Likely next moves [inference]
So what: where we win, where we lose, what to do
Refresh triggers
```
