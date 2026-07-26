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


---

# RESULTS (2026-07-25, gpt-5.4-mini, 4 phases + 4 confirmation approvals)

## Pass/fail bars — ALL MET

- Service boots + its own tests pass untouched: PASS (4/4 independently re-run).
- End-to-end in a real browser: PASS — game served same-origin at
  `/game.html`, telemetry rows land, Top-10 panel renders live data
  (screenshot lb3_panel_fixed.png). Zero console errors.
- **Zero unverified fix claims** (round 1: three). The executed-reproduction
  prompt rule visibly changed behavior: phase 4's FIRST action was probing
  for browser tooling, then it authored `service/test_browser_telemetry.py`
  and iterated it under pytest before claiming the fix.
- Memory: phase captures are clean single sentences; `game-backend` got
  corroborated → semantic_supersede (per-topic threshold working live).

## Phase notes

- P1 plan: textbook — enter_plan_mode → explore → write_plan →
  exit_plan_mode approval; "build nothing yet" respected post-approval.
  Plan added its own good constraint (game stays playable offline).
- P2 build: read_current_plan FIRST; files exactly per the plan's critical
  list; verification specialist ran and returned VERDICT: PASS through the
  natural resumability path (confirmation inside the sub-agent, resumed,
  handback, coordinator synthesis). Observation: overshoot — it also built
  the phase-3 game hooks ("build it" was read as the whole plan).
- P4 bug (CORS from file:// — invisible to its curl-based checks): correct
  diagnosis, architectural fix (same-origin `GET /game.html` bridge +
  permissive CORS), browser-facing regression test. One ~35-minute turn,
  ~87 tool calls, carried flawlessly by the durable-turn broker.

## Platform findings (adk-cc, this round)

- **F8 title-stanza leak — FIXED** (commit ff1d744; was cosmetic): coordinator sometimes emits the
  tool-title JSON (`{"title":"Calling verifier"}`) as a TEXT event —
  polluted final-reply extraction three times. Fix plan: suppress/strip
  Measured 128 occurrences in this project, ALL riding along with a call
  whose args already carried the same title → lossless to drop.
  ToolTitlePlugin.on_event_callback strips a text part that is exactly
  `{"title": "..."}`, guarded to events that also carry a function call so a
  real reply can never be blanked, returning a COPY (flow decides
  loop-vs-stop on the original).
- **F9 command-safety — FIXED** (commit ff1d744). The hypothesis above was
  WRONG; reproducing against the four real gated commands found three
  defects in `command_paths`, one a security hole:
  1. `/dev/null` was mined as an out-of-project path — `2>/dev/null` appears
     in ALL FOUR gated commands. Now an allowlist of write-safe devices;
     block devices stay mined + catastrophic.
  2. **SECURITY:** `_peel` DISCARDED leading `VAR=value`, so
     `KEY=~/.ssh/id_rsa; cat "$KEY"` mined ZERO paths and bypassed the
     protected-path deny floor (direct `cat ~/.ssh/id_rsa` is denied).
     Assignments now survive on ParsedSegment; values are mined and
     `$VAR`/`$HOME` resolve.
  3. The degenerate-parse fallback used the RAW command, re-admitting
     heredoc bodies that F6 excluded on the success path.
  Live-verified: a bypass-mode session (where this floor still applies) runs
  the exact shape with ZERO confirmations and the file written.
- Confirmation gates (4) were all reasonable (deletes/writes with variable
  or /tmp paths); resume-after-approval worked every time, including inside
  the verification specialist — the P3 stack validated repeatedly in
  unstaged conditions.

## Round-1 vs round-2 delta

| Metric | R1 (game) | R2 (service) |
|---|---|---|
| Unverified fix claims | 3 | **0** |
| Turns to working deliverable | 12 | 4 (+4 approvals) |
| Verification specialist used | never spontaneously | twice, meaningful |
| Plan mode | n/a | clean end-to-end |
| Memory capture | 1 silent capture, corrupted | per-phase, clean, consolidated |
