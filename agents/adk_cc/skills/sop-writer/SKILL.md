---
name: sop-writer
description: >
  Turn a real process into a runnable SOP — derived from the scripts and
  config that actually perform it, with preconditions, a verification per
  step, and rollback. Use to document a process or make a routine repeatable.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["every command in the SOP was read from real scripts/config/history rather than invented", "each consequential step has a verification the reader can run", "failure and rollback paths exist for steps that change state", "preconditions and required access are listed before step 1"]}
---

# SOP writer

An SOP is only useful if someone who has never done the task can follow it and
know, at each step, whether it worked. Most written procedures fail on the
second half: they list actions with no way to check them.

**Derive, don't imagine.** If the process exists in this workspace, read it out
of the repo before writing a line:

```bash
ls scripts/ Makefile justfile .github/workflows/ 2>/dev/null
grep -rn "<the thing>" --include=*.sh --include=*.yml --include=Makefile .
git log --oneline -20 -- <the relevant path>     # how it is actually done
```
A command you infer from documentation is a guess; a command you read from the
script that runs it is the procedure. Mark anything you could not verify as
`[UNVERIFIED — confirm before relying on this]`.

## Workflow

### 1. Frame it
- What is the trigger — a schedule, a request, an alert?
- Who performs it, and what access must they already have?
- What does "done" look like, observably?
- How often, and how bad is a mistake? (This sets how much verification to build
  in; a daily reversible task and a quarterly irreversible one deserve different
  SOPs.)

### 2. Preconditions block
Access, credentials (named, never valued), tools and versions, the state the
system must be in, and anything that must be true before starting. Include the
one-line check for each where possible.

### 3. Steps
Each step is: **action → expected result → how to verify**.

```
3. Apply the migration
   Run:      <exact command>
   Expect:   "<the actual string it prints>"
   Verify:   <command that proves it, e.g. a count or a status query>
   If wrong: <the specific recovery, or STOP and escalate to <role>>
```
Rules that make the difference:
- One action per step. A step with "and" hides a failure point.
- Exact commands, copy-pasteable, with placeholders in `<ANGLE_BRACKETS>`.
- Mark irreversible steps clearly — and put the backup/snapshot step before them.
- Where a step needs judgement, say what to judge on rather than "use judgement".

### 4. Failure handling and rollback
For every state-changing step: what does failure look like, what is the recovery,
and what is the point of no return. If there is no rollback, say so explicitly —
that is exactly the sentence someone needs to read before they run it.

### 5. Close the loop
End with: how to confirm the whole procedure succeeded, what to record (where,
in what form), and who to notify. Add a "last verified on / by" line — an SOP
with no freshness signal quietly rots.

## Output

```
# SOP — <process name>
Purpose · Trigger · Owner (role) · Last verified
## Preconditions (access, tools, state)
## Steps            ← action / expect / verify / if-wrong
## Rollback and points of no return
## Completion checks and records
## Notes and known gotchas
```

Prefer a short SOP that is exactly right over a long one that is mostly right.
