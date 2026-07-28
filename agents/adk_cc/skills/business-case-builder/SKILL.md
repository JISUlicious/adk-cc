---
name: business-case-builder
description: >
  Build a decision-ready business case — options including do-nothing, costs and
  benefits with stated assumptions, sensitivity on the assumptions that actually
  move the answer, and a recommendation. Use for buy/build/defer calls, budget
  requests, vendor selection, or justifying a project.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["a do-nothing baseline is costed, not just described", "every number traces to a stated assumption or a cited source", "sensitivity identifies which assumption flips the recommendation", "the recommendation names what would change it"]}
---

# Business case builder

A business case is an argument under uncertainty, not a spreadsheet. Its value
is in making the assumptions visible enough to argue with — so the reader can
disagree with an input rather than with your conclusion.

**No invented figures.** Every number is either computed here, supplied by the
user, or fetched from a source you cite with a date. Where a number is needed
and unavailable, use a clearly-labelled placeholder and put it at the top of
"what we need to know" — a plausible-looking fabricated cost is the single most
damaging thing this skill could produce.

## Workflow

### 1. Establish the decision and the decider
- What decision does this unblock, and by when?
- Who decides, and what do they optimise for (cash, risk, speed, headcount)?
- What is the organisation's context — size, stage, industry, cost structure?
  Ask if unknown; a case written for a 5-person startup is wrong for a 5,000-
  person enterprise in ways the numbers alone will not show.

### 2. Enumerate real options — always including do-nothing
At least three: **do nothing**, the proposal, and one cheaper alternative
(a partial, a manual process, an off-the-shelf tool). Do-nothing is not free —
cost it: the ongoing burden, the risk carried, the opportunity deferred. A case
that skips it is a sales pitch.

### 3. Cost each option honestly
- **Build/acquire**: effort, licences, migration, integration.
- **Run**: infrastructure, support, maintenance drag on the team.
- **Change**: training, downtime, temporary double-running.
- **Exit**: what it costs to reverse if it fails. Cheap-to-reverse options
  deserve a lower bar of proof — say so explicitly.

Where the work is software in this workspace, ground effort in the repo rather
than in industry averages (`git log --stat` on comparable past changes) — see
the `feasibility-study` skill for the calibration method.

### 4. Benefits — separate the measurable from the argued
| Type | Rule |
|---|---|
| Hard | Cash in or out, with a mechanism: "removes N hours/week at rate R" |
| Soft | Named, argued, and NOT summed into the total |
| Risk-reduction | Expressed as exposure × likelihood change, both stated |

Never total soft benefits into a headline number. That is how business cases
lose credibility with the person who has seen a few.

### 5. Sensitivity — the part that earns the case its keep
Vary each major assumption; report which ones flip the recommendation:

```
adoption 40% → 70%     : payback 19mo → 11mo   (does not flip)
labour rate -30%       : payback 19mo → 31mo   (FLIPS at ~25% below)
```
Two or three assumptions usually decide everything. Say which, and how confident
you are in them.

### 6. Recommend, and name your own kill criteria
State the recommendation in one sentence, the confidence, and the conditions
under which you would change it. Add a decision checkpoint: "revisit at N weeks
if adoption is below X".

## Output

```
# Business case — <decision>
**Recommendation** (1 sentence) · confidence · what would change it
## The decision and its deadline
## Options (incl. do-nothing)
## Costs — by option, with assumptions
## Benefits — hard / soft / risk, never summed together
## Sensitivity — what flips it
## Risks and exit cost
## What we need to know   ← placeholders, ranked by how much they matter
```
