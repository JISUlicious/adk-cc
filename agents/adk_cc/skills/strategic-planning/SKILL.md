---
name: strategic-planning
description: >
  Turn a strategy into a plan people can execute — the few bets worth making,
  measurable objectives with leading indicators, explicit trade-offs and what you
  are choosing not to do. Use for annual/quarterly planning, OKRs, or roadmap
  prioritisation.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["every objective has a measurable result with a named data source that exists", "an explicit not-doing list is included", "each bet states the assumption it rests on and the signal that would falsify it", "capacity is reconciled against the plan rather than assumed"]}
---

# Strategic planning

Strategy is choosing what not to do. A plan that funds every priority is a
budget, not a strategy — and it fails in the same way every time: everything
proceeds at 60% and nothing lands.

## Establish context before recommending anything

Ask, or state as assumed: **stage and size**, **market and geography served**
(what works in one market often does not transfer — distribution, buying
behaviour and competitive sets differ), **funding position and runway**,
**capacity** (real headcount and how much of it is already committed to
maintenance), and **the time horizon**.

Where a market, regulatory or industry-structure fact would change the plan, do
not recall it — `web_fetch` a source and cite it with a date, or mark it as an
assumption to be checked. General frameworks travel; specific facts about a
country's market do not.

## Work

### 1. Where are we, honestly
Two or three sentences of position: what is working, what is not, what changed.
Ground it in numbers the workspace can produce. This section is where planning
usually goes wrong — a plan built on a flattering picture optimises for the
wrong problem.

### 2. The few bets
Three at most for a quarter. For each:

```
Bet:        <what we are doing>
Because:    <the assumption it rests on>
Falsified by: <the signal that would tell us we were wrong>
Costs:      <the capacity it consumes>
```
The "falsified by" line is what separates a bet from a hope, and it is what lets
you stop early.

### 3. Objectives with results you can actually measure
Each objective gets 2–4 key results, each with a **named data source that
exists**. If nobody can produce the number today, the first key result is
building the measurement. Prefer leading indicators (activation rate, cycle
time) over lagging ones (revenue) at the team level — a team cannot steer by a
number that moves quarterly.

### 4. The not-doing list
Explicit, written down, with the reasoning. This is the highest-value part of the
document and the one most often omitted. Include what you are stopping, not only
what you are declining to start.

### 5. Reconcile with capacity
Sum the committed capacity against what exists after maintenance, support and
leave. If it exceeds 100%, cut a bet — do not shrink all three. Show the
arithmetic; an overcommitted plan discovered in week six costs a quarter.

### 6. Cadence
When you review, what evidence triggers a change of course, and who decides.

## Output

```
Position — where we are, with numbers
Bets — because / falsified by / cost
Objectives and key results — each with its data source
NOT doing — and why
Capacity reconciliation (the arithmetic)
Review cadence and decision rights
Assumptions to verify  ← including anything market- or region-specific
```
