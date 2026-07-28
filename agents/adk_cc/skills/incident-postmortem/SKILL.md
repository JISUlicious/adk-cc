---
name: incident-postmortem
description: >
  Write a blameless postmortem from actual evidence — reconstruct the timeline
  from logs, commits and deploys, separate trigger from cause, and produce action
  items that would have prevented it. Use after an outage, data loss, failed
  deploy, or any "how did this happen?" review.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["every timeline entry cites a real source (log line, commit, deploy record) rather than recollection", "trigger and contributing causes are stated separately", "each action item names an owner-shaped role and would have prevented or shortened THIS incident", "detection and recovery times are derived from evidence, not estimated"]}
---

# Incident postmortem

A postmortem is worth writing only if it changes something. The two failure
modes are a narrative with no evidence, and a list of action items that would
not have prevented the incident you just had. This skill exists to avoid both.

**Blameless means causal, not vague.** Name systems, decisions and gaps
precisely; do not name a person as the cause. "The deploy skipped staging"
is blameless *and* specific. "Mistakes were made" is neither.

## Workflow

### 1. Establish scope before writing anything
Ask, or state what you assumed:
- What broke, for whom, and how was it noticed?
- What is the window — first impact to full recovery?
- What is the reader for (an internal review, a customer-facing RCA, both)?

### 2. Reconstruct the timeline from evidence
Do not write the timeline from the incident chat's summary. Rebuild it:

```bash
# what shipped near the window
git log --since="<start>" --until="<end>" --format='%h %ad %an %s' --date=iso
git log -S"<suspect symbol>" --format='%h %ad %s' --date=short | head

# what the system said
grep -nE "ERROR|FATAL|panic|timeout" <logfile> | sed -n '1,80p'
```
Every timeline row gets a source: a log line, a commit SHA, a deploy record, a
dashboard timestamp. Rows you cannot source are marked `[unsourced]` — leave
them in, marked, rather than silently presenting them as fact.

Derive, do not estimate: **time to detect** = first impact → first human
signal; **time to recover** = first human signal → verified recovery. If the
evidence cannot support a number, say so.

### 3. Separate trigger from cause
- **Trigger** — what made it happen *now* (a deploy, a traffic spike, a cert
  expiry). Usually the easiest to find and the least useful to fix.
- **Contributing causes** — what made the trigger able to cause damage
  (missing validation, no rollback path, an alert nobody owned, a retry storm).
- **Why it lasted** — detection and recovery are separate failures from the
  break itself, and they are often the cheapest to fix.

Ask "why" until you reach something you can change. Stop when the next "why"
leaves the system you control.

### 4. Action items that pass the counterfactual test
For each item, state plainly: *had this existed, this incident would have been
prevented / detected in X / recovered in Y.* An item that fails that test is not
an action item — it is a wish. Prefer:
- a guard in code or config over "be careful next time";
- an alert with an owner over "monitor better";
- a rollback or kill-switch over "test more thoroughly".

Give each item a role-shaped owner and a rough size. Do not invent names.

## Output

```
# Postmortem — <short title>
**Impact**: who/what, how many, how long
**Detected**: <how> · time to detect <T>  ·  **Recovered**: <how> · time to recover <T>

## Timeline (all times <TZ>)
| Time | Event | Source |

## Trigger
## Contributing causes
## Why it lasted this long
## What went well          ← keep; it protects the practices worth keeping
## Action items
| Item | Prevents / shortens | Owner (role) | Size |
## Open questions          ← what the evidence could not answer
```

Keep "Open questions" honest. A postmortem that pretends to certainty it does
not have teaches the wrong lesson.
