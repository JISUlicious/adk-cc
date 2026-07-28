---
name: feasibility-study
description: >
  Assess whether a proposed feature or project is buildable here — approach
  options, an effort estimate calibrated on this repo's own history, risks,
  and a build/buy/defer call. Use before committing to significant work.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["the estimate is calibrated against named comparable commits in THIS repo", "a range is given with the driver of the spread named", "assumptions are listed explicitly", "integration, testing, migration, review and rollout are accounted for"]}
---

# Feasibility study

You answer "can we build this, at what cost, and should we?" — grounded in the
**actual codebase**, not in generic engineering averages. That grounding is the
whole value: an estimate calibrated on how long similar changes really took in
this repository beats any expert guess.

## Workflow

### 1. Pin the question
Restate the proposal in one paragraph, and name what is explicitly **out** of
scope. Most estimate disputes are scope disputes wearing a costume. If the
proposal is vague, list the interpretations and estimate the one you choose,
saying so.

### 2. Establish the ground truth
Before estimating, learn what exists:
- Which modules would this touch? (`grep`, `glob_files`, read them)
- Is there a partial implementation, an abandoned branch, a related abstraction?
- What are the integration points and their contracts?
- What constraints are already baked in (framework, data model, deploy target)?

### 3. Calibrate on this repo's own history — do not skip this
```bash
# how big was a comparable past change, really?
git log --oneline --since=12.months -- <related path> | head -20
git show --stat <commit-of-a-similar-feature> | tail -5
```
Convert to a local unit: "the last comparable feature touched 14 files / ~600
lines / landed over 3 weeks." An estimate anchored to that is defensible;
"about two weeks" is not.

### 4. Options, not a single path
Give 2–3 approaches. For each: sketch, effort, risk, and what it forecloses.
Always include the **cheapest thing that could work** — often a manual process,
a config change, or buying it. Say what each option makes harder later.

### 5. Estimate with uncertainty
- Decompose to tasks that are ≤2 days each; anything larger is a bundle you
  haven't understood yet, and it is where estimates fail.
- Give a **range** (optimistic / likely / pessimistic) and name the driver of
  the spread. A single number is a false promise.
- State assumptions explicitly — each is a place the estimate breaks.
- Add integration, testing, migration, docs, and review. The build is rarely
  the majority of the work.
See `references/estimation.md`.

### 6. Cost model
Engineering time × loaded rate, plus infrastructure/licences/vendor costs, plus
ongoing maintenance (a rough annual % of build cost). Compare against the value
claim if one was given — and say when the value claim is unverifiable.

### 7. Risks and the recommendation
Name the top risks with mitigations and, for each, what early signal would tell
you it's materializing. Then commit to a recommendation: **build now / build
reduced scope / prototype first / buy / defer** — with the condition that would
change your mind.

## Anti-patterns

- Estimating without reading the code the change lands in.
- A single number with no range and no assumptions.
- Ignoring maintenance: shipping is the down payment.
- "It depends" with no decision — you were asked to make a call; make it, and
  state what would change it.
- Padding silently. If you add a buffer, label it and explain why.
