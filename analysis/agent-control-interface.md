# Agent-control interface for adk-cc — concept & design notes

**Status: PARKED (2026-07-19).** Captured from a design discussion, deferred by user
decision — revisit later. **Open decisions below are UNRESOLVED; do not implement
without settling them first.** This is a concept doc, not an approved plan.

Companion doc: [`remote-access-gap-analysis.md`](./remote-access-gap-analysis.md) —
that one is about *humans* reaching sessions from other devices (mobile/web/messaging).
This one is the different axis: an *autonomous driver* (Claude, Codex, a script)
operating an adk-cc session as a first-class peer.

---

## The concept

Let an external driver — Claude Code, Codex, any script/agent — **control an adk-cc
session with the same capability surface a human has in the UI** (send messages, watch
the turn, see tool calls, answer permission prompts, read files, undo, switch modes),
**without adk-cc being embedded inside the driver** as a plugin/MCP server. adk-cc stays
a standalone peer; the driver reaches it through affordances it already has (a shell, an
HTTP client).

Two facets, both parked here:
- **Remote-controlling adk-cc by an agent** — a driver (possibly on another machine)
  operates a session end to end.
- **Agent-supervising-agent** — the driver answers adk-cc's HITL permission prompts
  (approve/deny), i.e. one agent supervises another's dangerous actions.

## Key finding: the substrate already exists

"Drive it like a user" is **not a new engine** — it's a driver-ergonomic surface over an
event log adk-cc already keeps:

- **Sessions are event-sourced JSONL** — `agents/adk_cc/service/file_session_service.py`
  (`<base>/projects/<user_id>/sessions/<id>.jsonl`, header line + one JSON `Event` per
  line). That IS an append-only log addressable by offset — the cursor substrate a
  driver needs.
- **Run surface is ADK's `/run_sse`** (+ `/run`, `/list-apps`,
  `/apps/{app}/users/{user_id}/sessions/{sid}/...`) — see `service/server.py`.
- **HITL is already resolved by sending a message.** `web/src/shared/api/sse.ts`
  `streamFunctionResponse` submits a `functionResponse` part carrying `callId` +
  `toolName` + `response` (`{chose_id, comment?, persist_across_sessions?}` for a
  confirmation). The agent loop picks it up on the next turn. A driver answering an
  approval is the SAME mechanism the UI uses.
- Session CRUD: `web/src/shared/api/sessions.ts` (`createSession` / `listSessions`).

So the build is a **surface + auth + driver ergonomics**, not a rewrite.

## Recommended shape (leaning, not decided)

**HTTP control API as the substrate (source of truth) + a thin CLI as the agent-facing
porcelain. Both.** The API is also what the mobile/relay work would consume; the CLI is
what makes it trivially drivable — Claude Code drives adk-cc by running `adk-cc send …`
in its **own bash tool**, zero plugin, zero coupling. The two agents compose *through the
shell*.

**Why not a plugin / MCP server (the thing being explicitly rejected):** embedding adk-cc
as an MCP server the driver loads makes adk-cc's turns tool-calls *inside the driver's*
turn, governed by the driver's loop — loses the peer framing, couples to MCP's RPC shape
(poor fit for long-running + streaming + approval-laden sessions), and every controller
must install the plugin. A CLI/API is driven by anything with a shell or HTTP client. (A
thin *optional* MCP shim could be added later for drivers who prefer it — adk-cc never
depends on it.)

## The turn/approval model for an agent driver

Core primitive: the **event log addressed by a monotonic cursor** (the JSONL already is
one). Three views over it:

1. **Blocking send** — `adk-cc send --session S "…" --wait` → block until a *stable
   state*, return structured JSON. The 80% case.
2. **Async + poll** — `send` returns a turn id; `poll --since <cursor>` returns new
   events. For long turns / interleaving other work.
3. **Wait-until** — `adk-cc wait --session S --until idle,needs-approval,error` — the
   single most useful command for an agent loop: blocks, then says *why* it returned so
   the driver branches without parsing a stream.

Ergonomics that matter because **the driver is itself an agent that may be re-spawned
between actions** (it can't hold a live SSE connection reliably):
- Cursor query ("everything after event N") is first-class — a browser holds a stream; an
  agent can't.
- **Exit codes encode state** (`0=idle`, `10=needs-approval`, `20=error`) so a
  shell-driving agent branches on `$?`.
- `--json` stable output by default; also offer a rendered human transcript view from the
  same log (both are cheap; a driver wants structured for control logic + rendered for
  its own reasoning/logging).
- Blocking `wait` naturally paces the driver against adk-cc's model rate-limit (see
  [[feedback_model_rate_limits]]) — no busy-polling the model.

## HITL — agent-supervising-agent + the safety line

The API surfaces a pending confirmation as **structured data** (tool, resolved
command/path, danger tier, offered choices) and blocks; the driver answers
`adk-cc respond --call <id> --choice allow-once`. Unlocks: Claude Code evaluates a
command adk-cc proposes and approves/denies it — one agent supervising another.

**The load-bearing safety invariant (recommended):**

> The permission engine's **hard-deny floor is non-bypassable, even by the driver.** A
> driver token may resolve **ask**-level prompts; it **cannot** override a **deny**
> (catastrophic command, protected-path secret read) — those still require a human or a
> separate escalation.

This reuses existing machinery (the run_bash danger classifier + protected-path floor,
see [[project_command_safety]]) as the backstop, so handing an autonomous agent a driver
token can't escalate past `rm -rf /` / `cat ~/.ssh/id_rsa`. Without this line, a full
driver token is the OpenClaw delegated-authority footgun (any holder induces arbitrary
tool calls).

## Auth — depends on desktop auth (P0)

A driver token *is* a full-session user credential. Requirements:
- **Scoped, revocable API token**, distinct from the human's Bearer token — scopes for
  *which sessions/projects*, *read-only vs steer vs approve*, and an *approval ceiling*
  (ask-only vs full).
- **In desktop mode this is the first real auth adk-cc gets.** An agent-control API on the
  loopback-no-auth sidecar is a remote-code-execution surface with no lock (the OpenClaw
  exposure). So **agent-control DEPENDS ON P0 (desktop auth/pairing)** from the
  remote-access plan — shared prerequisite.
- **Audit every driver action by token identity** — "who sent / who approved" matters
  doubly when an agent drives an agent.

## Transport

- **Same machine** (you run Claude Code locally, it drives the adk-cc desktop sidecar) →
  loopback API, shippable as soon as auth exists. Likely the primary case.
- **Remote driver** (Codex cloud, another box) → reuses the **P3 relay** from the
  remote-access plan.

Composition worth noting: a driver with full session access can also pick the
**workspace**, including the just-shipped SSH remote workspaces
([[project_ssh_remote_workspace]]) — so a driver could spin up an adk-cc session bound to
a remote device and drive real work there, entirely through the shell.

---

## OPEN DECISIONS (unresolved — settle before building)

1. **Primary driver location** — same-machine loopback (ship-now) vs remote (relay
   first). *Lean: local-first.*
2. **Approval posture for an agent driver** — driver resolves *ask* / human keeps *deny*
   (recommended) vs full-delegation-with-floor vs escalate-to-human path.
3. **Concurrency** — exclusive lease (human sees "controlled by agent X", can take over)
   vs human+agent multiplayer co-steering. *Lean: lease.*
4. **Surface priority** — CLI-first (fastest path to Claude-Code-drives-adk-cc) vs
   API-first (mobile/relay reuse it). *Lean: API substrate + thin CLI in the same pass
   since the CLI proves the idea.*
5. **Framing check** — "like what the user sees": structured events vs rendered transcript
   vs both. *Lean: both, from the same log.*

## Dependencies / sequencing (when revisited)

- **Blocked on P0** (desktop auth/pairing) — cannot expose before a lock exists.
- Reuses the permission-engine floors (already shipped) as the approval backstop.
- Local transport ships independently; remote reuses P3 relay.
- Roughly: P0 auth → agent-control API over the existing session core → thin CLI →
  (optional) MCP shim / remote via relay.
