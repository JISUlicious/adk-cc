---
name: prd-writer
description: >
  Write a product requirements document grounded in the actual codebase and
  product — problem, users, goals and non-goals, requirements, success metrics,
  and open questions. Use when specifying a feature, writing a PRD or spec, or
  turning a rough idea into something a team can build.
---

# PRD writer

A PRD earns its keep by removing ambiguity before it becomes rework. You write
one that an engineer can build from and a stakeholder can approve — grounded in
what the product **actually does today**, which is the advantage of writing it
here rather than in a blank document.

## Before writing: read the product

Do this first; it is what separates a useful PRD from a template fill-in.
- What exists already? (`grep`/`glob_files` for related features — half of PRDs
  re-specify something that is partly built.)
- What are the current data models, and what would this change?
- What are the existing UX patterns this must be consistent with?
- What constraints are already committed (framework, deploy target, auth model)?

Cite what you found. "Sessions already persist via `FileSessionService`, so X is
a small extension" is worth more than a page of prose.

## Structure

1. **Problem** — whose problem, evidence it exists, what happens if unsolved.
   No solution language here.
2. **Users** — who specifically, what they do today (the workaround is the
   strongest evidence a problem is real).
3. **Goals / Non-goals** — non-goals are the highest-value section: they end
   scope arguments before they start. Name the tempting things you are *not*
   doing and why.
4. **Requirements** — numbered, testable, prioritized (Must/Should/Could). Each
   should be checkable as done or not done by someone who wasn't in the room.
5. **UX** — key flows and states, including empty, loading, error, and
   permission-denied. Most bugs live in the states nobody specified.
6. **Technical notes** — integration points, data-model changes, migration,
   rollout/flagging. Reference real files.
7. **Success metrics** — how you will know it worked, measurable, with a
   baseline. "Users like it" is not a metric.
8. **Open questions** — with an owner and a decision deadline each.

## Rules

- **Requirements are testable.** "Fast" is not a requirement; "p95 under 300ms
  for 10k rows" is.
- **Separate must from nice.** Everything-is-P0 is the same as no priorities.
- **Write the non-goals.** If you write nothing else well, write these.
- **State the unknowns** rather than papering over them with confident prose.
- Keep it as short as the decision allows. Length is not rigor.

## Anti-patterns

- Specifying the implementation instead of the requirement (over-constrains
  engineering, and dates the document).
- Inventing user needs unsupported by evidence — say "assumed, unvalidated".
- Metrics that cannot be measured with what you have.
- Ignoring the existing codebase and specifying a parallel universe.
