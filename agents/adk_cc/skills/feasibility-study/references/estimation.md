# Estimating without lying

## Anchor locally, not globally
Industry averages are useless here. Find 2–3 comparable changes in *this* repo
and measure them (files touched, lines, calendar time from first to merge
commit). Express the new work as a multiple of those.

## Decompose until boring
Break to ≤2-day tasks. Anything bigger hides unknowns. If you cannot decompose a
piece, that piece is **research**, not implementation — schedule a timeboxed
spike and estimate the rest.

## Ranges and their driver
Give optimistic / likely / pessimistic, and name what drives the spread — usually
one specific unknown ("does the existing session store support X?"). Resolving
that unknown is often worth a day, because it collapses the range.

## The forgotten work
Routinely 40–60% of the total, and routinely omitted:
- integration with existing code that resists
- tests (including fixing ones this breaks)
- data migration and backfill
- docs, changelog, release
- code review cycles and rework
- deployment, feature flags, rollback plan
- observability: how will you know it works in production?

## Assumptions register
Every estimate rests on assumptions. List them. Each one is a tripwire — and the
list is what makes the estimate honest rather than a promise.

## When you cannot estimate
Say so, and say what you would need. "Two days of spiking on X, then I can
estimate the rest to ±30%" is a professional answer. A fabricated number is not.
