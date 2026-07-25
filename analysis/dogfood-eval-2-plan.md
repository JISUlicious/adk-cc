# Dogfood eval 2 — build a real service with adk-cc

2026-07-25. Round 1 (pixel-rogue, 12 turns on gpt-5.4-mini) graded the agent
on single-file frontend iteration and verification honesty. Round 2 targets
what that never touched: multi-file structure, backend+tests, plan mode,
sub-agent delegation, task tracking, and — the adk-cc-unique angle —
cross-session memory. It also evaluates the machinery landed since:
durable turns on long builds, the executed-reproduction prompt rule, and the
repaired memory capture.

## Build target

**A leaderboard + telemetry service for pixel-rogue**, then wire the game to
it. Continuity makes it a fair memory test (the agent has prior context to
recall) and gives the artifact a purpose.

- FastAPI + SQLite: `POST /scores` (run stats: slices cleared, gold, buffs,
  cause of death), `GET /leaderboard`, `GET /stats/summary`.
- Real test suite (httpx against the app), README with run instructions.
- Game integration: `game.html` posts a run on death/checkpoint; a
  leaderboard panel renders the top 10.
- Stretch: a daily-seed mode so runs are comparable.

## Session/phase structure (each phase = a NEW session, same project)

| Phase | Session | What it evaluates |
|---|---|---|
| 1. Plan | fresh session, **plan mode forced** ("plan first, don't build") | enter_plan_mode flow, plan quality/scope, exit-plan approval handling |
| 2. Build API | fresh session | plan recall (read_current_plan / memory), multi-file scaffolding, test-first discipline, task_create usage, verification sub-agent on a backend |
| 3. Integrate game | fresh session | cross-session memory: does it recall the game's code layout + service decisions without re-reading everything? edit discipline in an existing 1.1K-line file |
| 4. Break/fix | same session as 3 | executed-reproduction rule: report one real bug tersely, check it reproduces BEFORE claiming a fix (the round-1 failure pattern, now instruction-gated) |

Message discipline unchanged: ≤3 short-medium sentences per turn, concise
and slightly ambiguous. Model: gpt-5.4-mini again (comparability with round
1); optionally re-run phase 2 on sol for a tier comparison.

## Metrics (per turn, from session events — same collector as round 1)

- Turn duration, model events, tool-call histogram, sub-agent transfers.
- **Verification honesty rate**: fix claims backed by an executed repro
  (the new prompt rule's live test — round 1 scored 3 unverified claims).
- **Memory leverage**: does phase 3 use recalled facts (check recall
  injection + whether it re-explores what memory already knows)?
- **Plan adherence**: phase-2 build vs the phase-1 plan diff.
- Capture quality per turn (post-fix): facts/turn, malformed rate — should
  be clean single sentences now.
- End quality: tests pass, service runs, game actually posts scores
  (Playwright: die in game → row appears in leaderboard panel).

## Pass/fail bars

- Service boots + its own tests pass without my intervention: REQUIRED.
- Phase-3 integration works end-to-end in a real browser: REQUIRED.
- Zero unverified fix claims across phases 3-4: the headline metric.
- Memory: at least one phase-3 decision correctly recalled from phases 1-2
  without prompting (inspect recall block + behavior).

## Logistics

- New scratch project dir bound via the desktop projects API (same flow as
  round 1); broker-driven turns; Playwright for game-side verification.
- Sessions run with memory ON (default env) — capture/recall are part of the
  eval, not noise.
- Findings ledger appended to this file per phase; adk-cc platform bugs get
  the usual root-cause → fix → test treatment inline (per standing
  instruction).
