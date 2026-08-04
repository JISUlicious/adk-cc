# Long-running processes: start, watch, keep, kill

Status: PLAN v2 (2026-08-04), task #108. Scoped by the user's decisions:
**(1)** agent work only, and only LONG-RUNNING things — an API server, a
monitoring script — not every `ls`; **(2)** background processes are
first-class: start a dev server, keep it alive across turns, watch its log;
**(3)** start with local processes in desktop mode, investigate remote
backends properly.

This replaces the v1 "track every exec" draft. Tracking every command was the
wrong shape: the value is in the handful of processes that OUTLIVE a turn,
and those need a different lifecycle than a `grep`.

## What exists today (investigated)

Every agent-run command reaches the OS through `SandboxBackend.exec` /
`exec_stream`. Relevant facts, verified in the code:

- **Every local exec already gets its own process group** (`start_new_session
  =True`), and a timeout kills the whole **group** (`_kill_group`) — added
  because killing the shell alone orphaned background children.
- **`timeout_s` is the only way a command ever dies.** There is no cancel.
- **`run_bash` actively discourages background work today**: its instruction
  tells the model that if it starts a background process it must redirect
  that process's output. So the current answer to "run a dev server" is a
  workaround, not a feature.
- Output is capped (`_MAX_STREAM_BYTES`) while continuing to drain — a flood
  can neither OOM the server nor deadlock the child.
- `.adk-cc/` is excluded from checkpoints (#104), so process logs can live
  there without polluting undo history.

### The lifecycle trap this feature creates

A backgrounded process is deliberately NOT in the turn's lifetime — which
means it also escapes every cleanup we have. Worse, `start_new_session=True`
puts it in its own process group, so it survives the backend dying too.
#98 fixed exactly this class of bug for the app→backend relationship (the
parent watchdog + group kill); this feature must not reintroduce it one
level down. **Ownership and reaping are P0 concerns, not P4 polish.**

## Design

### The tool surface

`run_bash(command, background=True, label="dev server")` — returns
IMMEDIATELY with a process id and the first moments of output, instead of
blocking to a timeout. Rationale for extending `run_bash` rather than adding
a `start_process` tool: the model already reaches for `run_bash`, and a
second tool competing for the same intent is how you get neither used well.
The tool description flips from "redirect output yourself" to "use
`background=True` for anything long-lived; it is tracked and you can read its
log."

Companion tools, deliberately few:
- `list_processes()` — what is running in this session (also visible to the model, so it stops re-starting a server it already has).
- `read_process_log(id, tail=…)` — how the agent checks "did the server come up".
- `stop_process(id)`.

### P0 — registry, logs, ownership (desktop/local)

`agents/adk_cc/sandbox/process_registry.py`, keyed by session:

```
id, session_key, project_id, label, command (redacted), cwd, backend,
pid, pgid, started_at, status (starting|running|exited|killed|failed),
exit_code, log_path, ports (best-effort), can_terminate
```

- **Logs are FILES, not memory**: `.adk-cc/processes/<id>.log`, append-only,
  size-capped with rotation. They outlive the turn, the session, and a
  backend restart — which is the entire point. (Memory ring buffers die with
  the process that owns them; that is fine for a 30s command and wrong here.)
- **Registry persists** to `.adk-cc/processes/index.json` so a backend restart
  can re-adopt or at least honestly report what it lost.
- **Ownership/reaping (the trap above):**
  - Every background process records its `pgid` at launch.
  - The backend reaps its session's processes when the session is deleted
    (#87's abort hook) and on graceful shutdown.
  - **Orphan sweep at boot**: on startup, read the index, and for each
    recorded pgid decide — still alive and ours (adopt), gone (mark exited),
    or alive but re-parented/unknown (report, offer a kill). This is #98's
    lesson applied one level down.
  - Explicit policy, surfaced in the UI: a background process **survives
    turns and sessions**, but **not** the app quitting. Anything else is a
    footgun on a desktop app.

### P1 — control

`terminate(id)`: TERM the process **group** → 3s grace → KILL, reusing
`_kill_group` (already the tested timeout path). The tool result reports a
clean "terminated by user" so the model sees a legible outcome.

### P2 — API

```
GET  /api/processes?session_id=…        list (running first, then recent)
GET  /api/processes/{id}                detail
GET  /api/processes/{id}/log?tail=N     log tail
GET  /api/processes/{id}/stream         SSE follow (tail -f)
POST /api/processes/{id}/terminate
```

### P3 — UI

- **Process dock** in the right panel footer, modelled on `SubagentsDock`
  (same polling discipline): one row per process — label, status, elapsed,
  port if detected, Stop. Unlike the sub-agents dock it persists across
  turns, because the processes do.
- **Log drawer**: live-following console reusing `BashTerminalCard`'s
  renderer rather than a second terminal look.
- A detected port becomes a clickable `http://localhost:<port>` — the single
  most useful thing for a dev server.

### P4 — remote backends (investigated; here is the honest picture)

The generalizable mechanism: **for any backend that can exec, terminate is
just another exec** — `kill -TERM -<pgid>` — provided the pgid was captured
at launch (wrap the command so it prints its own pgid on the first line).
That is one mechanism, not five per-backend APIs.

| Backend | Terminate | Notes from the code |
|---|---|---|
| **noop / local (desktop)** | ✅ direct `killpg` | P0 target; already the tested timeout path |
| **local_container** | ✅ | already runs `timeout` INSIDE the container, so in-container signalling works; `docker kill` as backstop |
| **ssh** | ✅ via pgid + a second channel | today's timeout is CLIENT-side only and the code documents that "the remote command may keep running" — a known v1 limitation this would actually fix |
| **daytona** | ⚠️ via exec `kill` | no per-exec cancel in the API (only whole-sandbox DELETE, which is nuclear); the pgid trick should work through its exec endpoint — needs a live probe |
| **sandbox_service** | ❌ for now | no cancel primitive exists service-side; needs an endpoint there. Report `can_terminate: false` rather than showing a button that lies |

So: honest capability flags, one shared mechanism, and `sandbox_service` is
the only genuine gap.

## Phasing

| Phase | Deliverable | Verification |
|---|---|---|
| P0 | registry + file logs + `background=True` + ownership/orphan sweep (local) | live: start a dev server, confirm it survives a turn AND a new session; kill the backend, confirm the boot sweep reports it |
| P1 | terminate + `stop_process` + tool results | live: `python -m http.server`, Stop, verify group gone via `ps` |
| P2 | endpoints + SSE follow | live: follow a monitoring script's log through the API |
| P3 | dock + log drawer + clickable port | Playwright: start a server, see the row and port, click Stop |
| P4 | container + ssh (pgid mechanism), daytona probe, honest flags | live on the SSH box from the remote-workspace work |

## Deliberately NOT in scope

- Restart/supervision (a crashed server stays dead; the agent can restart it).
- Port allocation or conflict resolution beyond *detecting* a port.
- Tracking short commands. If a foreground command's length becomes a
  problem, #105's elapsed/tool indicator already covers the visibility half.
