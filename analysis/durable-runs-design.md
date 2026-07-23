# Durable runs — owning the turn lifecycle

Design note for the F1 + F3-server + F2b/F2c cluster
(analysis/dogfooding-findings-fix-plan.md). Status: PROPOSED — review before
implementing.

## Problem

ADK ties run execution to the HTTP request: `/run_sse` consumes
`runner.run_async` inside the response generator, so a client disconnect
(timeout, tab close, refresh, laptop lid) cancels the turn mid-flight.
Completed tool calls persist; everything after — including the agent's final
reply — silently never happens. Three shipped findings are facets of this:

- **F1**: turns die on disconnect (observed twice during dogfooding).
- **F3-server**: a confirmation-resume roots the run at the sub-agent; when
  it hands back, the run ends with no coordinator continuation. The shipped
  client auto-continue only covers the web UI.
- **F2b/F2c**: "retry turn" needs an owner that can reuse (not re-append)
  the user event; today every failed attempt appends a duplicate user
  message because the runner persists `new_message` before the model call.

## Design: a per-process Turn Broker

Runs execute as **server-side tasks**; SSE becomes a **tail**, not the
executor. Additive — ADK's `/run_sse` stays untouched as a fallback.

### Components (`service/turns.py`)

- **TurnBroker** — registry `{turn_id → Turn}`; **single-flight per session**
  (starting a turn while one runs → 409). Finished turns GC'd after ~10 min.
- **Turn** — an `asyncio.Task` driving `runner.run_async(...)` TO COMPLETION
  regardless of subscribers. Each event is (a) persisted by the session
  service as today, (b) appended to an in-memory buffer, (c) broadcast to
  subscribers (`asyncio.Condition`). Terminal state records status
  (`done | error | aborted`) plus a classified error payload (reusing
  `models/rate_limit.classify_429` output so the UI can render "retry in
  ~Ns" vs "switch model").

### Endpoints (mounted in `build_fastapi_app`)

```
POST /api/turns                {app,user,session,newMessage} → {turn_id}   (409 if session busy)
GET  /api/turns/{id}/stream    SSE tail; ?cursor=N replays buffer from N then live-tails
GET  /api/turns/{id}           {status, cursor, error?}
POST /api/turns/{id}/abort     cancel the task (explicit abort still works)
GET  /api/turns?session=…      the session's active/latest turn (reconnect-on-mount)
POST /api/turns/retry-last     {session} → re-run the last errored turn (see F2c)
```

### How each finding closes

- **F1**: disconnect loses the tail, not the work. Reopening the session
  finds the running turn (`GET /api/turns?session`) and re-attaches with a
  cursor. Laptop-lid / refresh / driver-timeout all become non-events.
- **F3-server**: when the task's run ends and the LAST event is a dangling
  `_handback_to_coordinator` (sub-agent author, no coordinator reply after),
  the broker immediately continues the same logical turn. v1 continuation is
  a synthetic minimal user message (what the client mitigation does, now
  covering ALL drivers); the client-side mitigation is then retired.
- **F2b**: the UI Retry button = `POST /api/turns/retry-last`.
- **F2c**: the broker knows a turn produced zero model-authored events; on
  retry it first prunes the orphaned user event where the session service
  supports deletion (our file session service — desktop's default — gains
  `delete_last_event`; ADK's DatabaseSessionService: phase 2, else the
  broker falls back to reuse-without-prune and the UI dedupes display).

## Open questions (resolve in P0 before building)

1. **Session-service sharing**: the broker's Runner must observe the same
   sessions as ADK's routes. Preferred: reach the instance ADK's
   `get_fast_api_app` built (via the AdkWebServer object / closure).
   Fallback: construct a second service on the same URI — persistence is the
   consistency contract (sqlite/file both fine single-process).
2. **ADK resumability**: `Runner(resumability_config=…)` exists in 1.31+.
   Investigate whether native pause/resume solves the F3 rooting problem
   properly (a resumed run that returns to the parent) — if yes, the F3
   continuation hack shrinks or disappears.
3. **Multi-worker web**: the broker is per-process. v1 scope: single worker
   (desktop always is; the web dev deployment is too). Document; a shared
   broker (redis/db-backed) is out of scope.
4. **Unobserved turns burn quota**: a disconnected turn keeps calling the
   model. Acceptable (it's what the user asked for) — bounded by
   single-flight + abort + the existing pacing throttle.

## Phases

- **P0** (investigation, ~half day): session-service sharing; resumability
  capabilities; confirm event shapes for the tail protocol.
- **P1** (core, ~300-400 LOC + tests): broker + endpoints. Tests use the
  scripted-LLM harness (no live models): disconnect mid-turn → turn
  completes + events persisted; reattach replays from cursor; abort
  cancels; single-flight 409; error classification surfaces.
- **P2** (UI, ~150 LOC): sse.ts broker client (legacy fallback kept);
  ChatPage reconnect-on-mount; abort via broker; Retry button (F2b).
- **P3** (~60 LOC): F3 server-side continuation in the broker; retire the
  ChatPage auto-continue.
- **P4** (~80 LOC): file-session `delete_last_event` + broker prune (F2c);
  sqlite pruning deferred.

## Non-goals

- Cross-restart turn survival (a server restart still kills running turns —
  session events persist, the tail reports `error`).
- Multi-worker brokering; background turns UI (queueing several turns).
- Changing ADK's own `/run_sse` (kept verbatim for compat).
