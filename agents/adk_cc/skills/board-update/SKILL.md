---
name: board-update
description: >
  Draft a board or investor update — metrics against plan, the honest problems,
  decisions actually needed, and asks. Establishes entity and governance context
  first; never states filing, notice or fiduciary rules from memory.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["the output opens with a context line naming entity type, jurisdiction and what is NOT ESTABLISHED", "every metric is computed from real data in this turn and shown against plan and prior period", "no statutory notice, quorum, filing or fiduciary requirement is asserted from memory", "bad news appears before the asks, with what is being done about it"]}
---

# Board / investor update

A good update is the one where nothing in the meeting is a surprise. Its job is
to give a decision-making body an accurate picture cheaply — which means the
problems go near the top, not in an appendix.

## Context first — governance is entity- and jurisdiction-specific

Board composition, notice periods, quorum, written-resolution mechanics, what
must be minuted, what requires board or shareholder approval, and what must be
filed **depend on entity form and jurisdiction**, and often on the company's own
articles and investor agreements. These are not general knowledge.

Ask, or mark explicitly as not established:
- **Entity type and jurisdiction** of incorporation (and where it operates, if
  different);
- **Body being addressed** — a formal board, an advisory board, or an investor
  distribution list. The obligations and the tone differ.
- Cadence, reporting period, and whether anything here is a **formal approval
  item** rather than information.

`Context — entity: NOT ESTABLISHED · jurisdiction: NOT ESTABLISHED · audience: investor update (informational) · period: Q2 2026`

**Never state a governance rule from memory** — not notice periods, not quorum,
not approval thresholds, not filing deadlines. If one matters, `web_fetch` a
current authoritative source, cite it with a date, and route it to counsel or the
company secretary anyway. Constitutional documents override general rules, and
you have not read them. This is **not legal advice**; anything with an approval,
minuting or filing consequence needs a qualified adviser.

## Content

### 1. Headline — three sentences
Where the business is, what changed since last time, what you need from them.

### 2. Metrics against plan
Compute them from real data in this turn; never carry a number forward
unverified. Each metric: actual, plan, prior period, and the reason for any
material gap. State definitions once and keep them stable — a redefined metric
between updates reads as a smokescreen, and often is one.

### 3. What is not working
Explicit, early, with what you are doing about it and what would change your
mind. Boards discover bad news eventually; the only variable is whether they
discover it from you. Include what you got wrong since the last update.

### 4. Decisions and asks
Separate **decisions needed now** (with the options, your recommendation, and
the consequence of deferring) from **asks** (introductions, hiring help,
expertise). Vague asks get vague help — name the person, company or skill.

### 5. Appendix
Detail that supports the above: cohort tables, pipeline, org changes, cash
detail. Runway from the cash line, with the assumptions that would move it.

## Output

```
Context line
Headline (3 sentences)
Metrics vs plan — with definitions and gap explanations
What's working
What's not — and the response
Decisions needed  |  Asks
Appendix
Open items for counsel / company secretary (if any approval or filing is implied)
```
