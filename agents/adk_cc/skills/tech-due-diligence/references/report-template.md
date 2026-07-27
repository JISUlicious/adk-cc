# Technical due diligence — report template

## 1. Executive summary
Decision this informs, overall risk rating, and the **three findings that would
change the decision**. Written for a non-engineer.

## 2. Snapshot
| Dimension | Finding |
|---|---|
| Languages / size | |
| Age / last commit / cadence | |
| Contributors (and concentration) | |
| Test suite (exists / runs / count) | |
| CI | |
| License posture | |

## 3. Findings
One block each, ordered by severity.

> **[SEVERITY] Title**
> **Evidence** — command + output excerpt, or `path/file.py:120`
> **So what** — the consequence in time, money, or legal exposure
> **Remediation** — what fixing it takes, roughly

Severity: **Critical** (blocks the deal / legal exposure) · **High** (months) ·
**Medium** (quarters of drag) · **Low** (hygiene).

## 4. What I could not determine
List explicitly — unverified areas are themselves a risk, and hiding them is how
diligence reports mislead.

## 5. Recommendation
Proceed / proceed with conditions / do not proceed — with the conditions named
and priced where possible.
