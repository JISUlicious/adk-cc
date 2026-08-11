# Session-scope knowledge: review + upgrade plan

Status: SHIPPED (2026-08-12) — P0 b1cd2cc+c8a38e7, P1-P3 e2d4dfc, tests a9f4632. Task #127 complete. (P2's compaction-seeding item was dropped as redundant: notes re-inject from state every turn.)

## Current state (verified this week)

1. Memory — USER scope (desktop: user = project). Capture prompt now
   mode-aware: web capture EXCLUDES session-scoped facts (d919b5b).
   Provenance: every item records its source session id; no session tier.
2. Wiki — DOMAIN scope with per-user inbox staging; librarian publishes.
3. Session scope — NO structured system. It lives implicitly in:
   transcript (lossy once event compaction summarizes), session state
   (machinery, not knowledge), workspace files. The capture fix sharpened
   the hole: "chose approach B; API paginates at 100" has no home.

## Options considered

(1) Session key in the memory store (users/<uid>/sessions/<sid>/…):
REJECTED — duplicates lifecycle machinery (needs its own reaper +
delete-cascade), second capture pass or per-fact scope routing, splits
the recall budget, autonomous where curation is wanted.

(2) Workspace file (AGENTS.md-style session notes): right instinct,
wrong backing — desktop in-place would pollute the project root; web
scratch reaping can race a still-resumable session.

(3) CHOSEN — session notes in SESSION STATE: one curated markdown note
per session, key `session_notes`, injected each turn. State already has
the exact right lifetime (travels with the session store, deleted with
the session, resume-safe, shell-agnostic). Precedent: the plan file is
already a curated per-session doc; this generalizes it to working notes.

## Division of labor (the doctrine)

- session → NOTES (explicit, curated, dies with the session)
- user    → MEMORY (autonomous capture/recall, survives sessions)
- team    → WIKI  (explicit staging, librarian-published domain pages)

## Plan

P0 — core:
  - `update_session_notes` tool (replace|append, ungated — it writes only
    its own session's state; size-capped ~2k tokens,
    ADK_CC_SESSION_NOTES_BUDGET, oldest-trimmed on append).
  - NotesPlugin: before_model injects "## Session notes" after the memory
    recall block, every turn, verbatim (no search — notes are small).
  - Tool description teaches the model WHEN: decisions, discovered
    constraints, task state worth surviving compaction. Tests: unit
    (cap, replace/append) + the load-bearing A/B — force compaction past
    a decision recorded ONLY in notes, resume, assert the model still
    knows it (the transcript alone must fail this).
P1 — capture routing (web): the capture pass, instead of DROPPING
  session-scoped facts, may emit them as NOTE suggestions
  (auto-append, deduped, flag ADK_CC_SESSION_NOTES_AUTOCAPTURE,
  default off — explicit-first, mirroring the wiki philosophy).
P2 — surfaces: notes section in the right panel (read first, edit later);
  compaction summarizer seeds from notes (like COMPACTION_SEED_MEMORY);
  /notes slash to view.
P3 — later: promote-to-memory affordance ("keep this beyond the
  session"), which is the clean answer to the missing mid-tier; if web
  ever grows projects, notes scoping already generalizes.

Non-goals: session tier in the memory store; auto-capture on by default;
any cross-session read of another session's notes.
