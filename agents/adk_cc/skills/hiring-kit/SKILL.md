---
name: hiring-kit
description: >
  Build a hiring kit for one role — job description, scorecard with observable
  signals, structured interview loop and calibrated questions. Establishes the
  employment context first and never states local employment rules from memory.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["the output opens with a context line naming jurisdiction, entity and what is NOT ESTABLISHED", "no notice period, probation length, contract term, benefit entitlement or pay figure is asserted from memory", "every scorecard line is an observable signal with how it will be evidenced", "each interview stage names what it is uniquely testing, with no duplicate coverage"]}
---

# Hiring kit

A good loop makes decisions comparable. Most hiring failures are not bad
judgement — they are four interviewers testing overlapping things, none of them
against a written bar.

## Context first — employment is jurisdiction-bound

Hiring rules are among the most country-, entity- and year-specific of any
business area. Probation, notice, working time, contract types, at-will vs
protected status, mandatory benefits, permissible interview questions,
background checks, and what may lawfully appear in a posting **all differ by
jurisdiction** — and several differ by region within one country.

So: **ask** for, or explicitly mark as not established:

- **Jurisdiction** where the person will be employed (not where the company is
  headquartered — remote hiring routinely separates the two);
- **Entity** doing the employing (own entity, subsidiary, employer-of-record,
  contractor engagement — the rules differ sharply between these);
- Employment type and level, team, and who the hiring manager is.

Open the output with a context line, e.g.
`Context — employing entity: NOT ESTABLISHED · jurisdiction: NOT ESTABLISHED — the loop below is portable; posting text, contract terms and any statutory item must be checked locally.`

**Never state a specific rule from memory.** If a local requirement matters
(what a posting must disclose, whether a pay range is mandatory, what a
background check may cover, whether a question is permissible), fetch the current
source with `web_fetch`, cite it with a date, and still route it to a human. This
is a drafting aid and **not legal advice**; employment counsel or a local HR
adviser should review anything that goes to a candidate or into a contract.

## Build

### 1. Role definition — outcomes, not a wish list
What must be true in 12 months for this hire to have been a success? Three to
five outcomes. Then the capabilities each outcome requires. Requirements that
trace to no outcome get cut — that is where inflated "years of experience" and
accidental exclusions come from.

### 2. Scorecard — observable signals
| Capability | What "meets the bar" looks like | Evidenced by (stage) |
Each line must be something an interviewer can *observe*, not infer: "designed a
migration under a compatibility constraint and can explain the trade-off", not
"strong engineer". Every capability is owned by exactly one stage.

### 3. The loop
Each stage names what it uniquely tests and what would fail a candidate. Keep
the same questions across candidates — comparability is the entire point. Prefer
work-sample and past-behaviour questions over hypotheticals; ask for the same
evidence from everyone.

### 4. Consistency and fairness
Same questions, same order, same scale, independent scoring before discussion
(the first opinion voiced anchors the room). Score against the written bar, not
against the other candidates. Debrief on evidence: "in stage 2 they did X",
not "I liked them".

## Output

```
Context line (jurisdiction / entity / NOT ESTABLISHED items)
Role outcomes → capabilities
Job description draft   [local posting requirements: VERIFY LOCALLY]
Scorecard — observable signals per capability, mapped to stages
Interview loop — stage, tester, questions, failure signals
Debrief and decision rule
Open items for HR/counsel
```
