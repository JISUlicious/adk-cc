# Process management: every command visible, followable, and killable

Status: PLAN (2026-08-04), task #108. Requirement: *all commands and scripts
run inside adk-cc must be tracked in the UI — list them, see console output,
and manage (terminate) them.*

## What exists today (investigated, not assumed)

**One chokepoint, which is the good news.** Everything the agent runs reaches
the OS through a `SandboxBackend`: `exec()` (buffered) or `exec_stream()`
(chunked). Callers:

| Caller | Path | Notes |
|---|---|---|
| `run_bash` | `backend.exec_stream()` | the only streaming caller today |
| skill scripts (`run_skill_script`) | `_WiderScriptCodeExecutor` → `backend.exec` | wrapper script, deps install |
| code execution / analysis | `code_executor` → `backend.exec` | content-addressed `scratch-<sha>.py` |
| analysis-env provisioning | `analysis_env` → `backend.exec` | probe + `uv` installs |
| checkpoints, file ops, ssh | direct `subprocess`/transport | infrastructure, NOT agent work |

Backends: `noop` (desktop, local `create_subprocess_shell`), `local_container`,
`sandbox_service` (SSE), `ssh`, `daytona`.

**Process hygiene already in place** (do not rebuild): every local exec gets
`start_new_session=True` (own process group), and a timeout kills the whole
**group** (`_kill_group`) precisely because killing the shell alone orphaned
background children. Output is capped (`_MAX_STREAM_BYTES`) while continuing
to drain, so a flood can neither OOM the server nor deadlock the child.

**What is missing, exactly:**
1. No registry — nothing knows what is running right now. A process exists
   only as a local variable inside one `exec` call.
2. No cancellation — `timeout_s` is the *only* way a command dies. A user
   watching a runaway `npm install` has no button; aborting the turn cancels
   the asyncio task, and the child's fate depends on the backend.
3. Output is per-call and post-hoc — `run_bash` streams into the thread card,
   but there is no place to follow a command already in flight, and nothing at
   all for skill scripts / analysis execs (buffered `exec`).
4. Nothing survives the turn — the thread is the only record, scattered across
   cards.

**Precedent to copy:** the sub-agents dock (`/api/subagents` +
`SubagentsDock`, registry keyed by session, polled 1.5s hot / 6s idle) is the
exact shape this needs, one level down.

## Design

### P0 — the registry (the load-bearing piece)

`agents/adk_cc/sandbox/process_registry.py`: process-global, keyed by
`app/user/session`, holding one record per exec:

```
id, session_key, kind (bash|skill|code|provision), label,
command (redacted), cwd, backend, started_at, finished_at,
status (running|done|failed|timed_out|killed), exit_code,
pid/handle (backend-specific), ring buffer of recent output
```

Registration happens in **one place** — a small wrapper around
`backend.exec` / `exec_stream` in the base class, so every caller above is
covered without touching five modules, and any future caller is covered by
construction. `kind`/`label` come from a contextvar the tool layer sets
(`run_bash` → the command's title; skills → skill name + script).

Retention: keep finished records for N minutes / M entries (same bounded-LRU
discipline as the summaries cache), so "what did that command print" survives
past the turn without unbounded growth.

Output: a ring buffer (default 256KB per process, tail-biased — the end is
what matters for a failure) fed by the same drain loop that already exists.
No new reading path, no new flood risk.

### P1 — control: terminate

`terminate(process_id, escalate=True)`: TERM the process **group**, 3s grace,
then KILL — reusing `_kill_group`, which already exists and is already the
tested behaviour for timeouts. Per backend:

- **noop/local**: direct `killpg`.
- **container**: `docker kill` the exec, or signal inside the container.
- **ssh**: the transport already multiplexes; send a signal by remote pgid.
- **sandbox_service / daytona**: needs a remote cancel endpoint — if the
  backend cannot cancel, the API must say so (`can_terminate: false`) rather
  than pretend. **No fake buttons.**

Cancellation must mark the record `killed` and let the *tool* return a normal
result ("terminated by user") so the model sees a legible outcome instead of
a hang or an opaque error.

### P2 — API

```
GET  /api/processes?session_id=…      list (running first, then recent)
GET  /api/processes/{id}              detail + output tail
GET  /api/processes/{id}/stream       SSE tail (live follow)
POST /api/processes/{id}/terminate    TERM→KILL, returns the new status
```
Auth/tenancy: same middleware as the rest; a process is visible to its own
session's owner only.

### P3 — UI

- **Process dock**, right panel footer, exactly where `SubagentsDock` lives
  (and reusing its polling discipline): one row per running command — kind
  icon, label, elapsed, a Stop button. Collapses when idle.
- **Detail drawer**: full command, cwd, backend, status, and a live-following
  console (reuse `BashTerminalCard`'s renderer rather than inventing a second
  terminal look).
- **Thread integration**: the existing bash card gains a Stop control while
  its command is in flight — the natural place to reach for it.
- Finished-recently section so a command that just failed is one click away.

### P4 — hardening

- Kill-on-abort: aborting a turn terminates that turn's still-running
  processes (today it cancels the task and hopes). Same lifecycle hook the
  sub-agent cleanup uses.
- Kill-on-session-delete (#87 already established the abort hook to piggyback).
- Audit events for user-initiated terminations.
- Redaction: commands can carry secrets — reuse the existing redaction used
  for skill/MCP env before storing or displaying.

## Phasing

| Phase | Deliverable | Verification |
|---|---|---|
| P0 | registry + base-class wrapper + ring buffer | unit: every exec path registers; bounded retention; redaction. Live: run bash/skill/analysis, see three records |
| P1 | terminate (noop first), `can_terminate` per backend | live: `sleep 300` from the agent, Stop, verify child AND group gone (`ps`), tool returns a clean "terminated" |
| P2 | endpoints + SSE tail | live: follow a long command's output through the API |
| P3 | dock + detail drawer + Stop in the bash card | Playwright: start a long command, see the row, click Stop, watch it clear |
| P4 | abort/delete kill-through, audit, redaction | live: abort a turn mid-command, confirm no orphan (`ps` before/after) |

## Open questions for you

1. **Scope of "all commands":** agent-run commands only (bash, skills,
   analysis) — or also adk-cc's own infrastructure execs (checkpoint `git`,
   file ops)? My recommendation: agent work only. Infrastructure execs are
   noise, and surfacing them invites terminating something load-bearing.
2. **Background processes:** today `run_bash` discourages them (the
   instruction tells the model to redirect output). Should this feature make
   long-running background processes a first-class thing — start a dev
   server, keep it alive across turns, watch its log in the dock? That is a
   genuinely bigger feature (lifecycle beyond the turn, port management) and
   I would do it as a follow-on, not P0.
3. **Remote backends:** accept "list-only, no terminate" for backends that
   cannot cancel (honest `can_terminate: false`), or block the feature until
   every backend supports it? Recommendation: ship honest capability flags.
