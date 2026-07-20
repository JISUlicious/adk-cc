# adk-cc

A Claude-Code-style **gather → plan → act → verify** agent loop built as a single ADK agent module (loadable by `adk web` / `adk run`), with an opt-in FastAPI factory for single-instance deployment and a custom React chat UI that ships alongside.

Read in: **English** · [한국어](./README.ko.md)

Deeper docs in [`docs/`](./docs/): [specification](./docs/01-specification.md), [architecture](./docs/02-architecture.md), [prompts](./docs/03-prompts.md), [sandbox runbook](./docs/04-deployment-sandbox.md), [production runbook](./docs/05-production-deployment.md), [confirmation wire protocol](./docs/06-confirmation-protocol.md), [web UI](./docs/07-web-ui.md).

## What it is

- One **coordinator** (the only agent that talks to the user). Acts directly with read tools (`read_file`, `glob_files`, `grep`, `web_fetch`, `read_current_plan`), write/exec tools (`write_file`, `edit_file`, `run_bash`), task tools (`task_create` / `task_get` / `task_list` / `task_update`), HITL tools (`ask_user_question`), plan-mode tools (`enter_plan_mode`, `exit_plan_mode`, `write_plan`), and any auto-loaded skills/MCP toolsets.
- Two specialists wired as ADK `sub_agents`: **`Explore`** (broad codebase search returning a written report) and **`verification`** (adversarial post-implementation gate ending with `VERDICT: PASS|FAIL|PARTIAL`). Delegation is `transfer_to_agent`; sub-agents share the parent's invocation context, so all tool calls/responses stream into the UI rather than being buried in an opaque tool result.
- **Planning is not a sub-agent.** When the user wants a written plan with approval before any change, the coordinator calls `enter_plan_mode` — a posture the coordinator takes. `PlanModeReminderPlugin` then dynamically filters write/exec tools out of the LLM's tool surface and injects a planning instruction. The plan persists via `write_plan`; `exit_plan_mode` gates re-entry to the unrestricted surface on user approval.
- Hub-and-spoke + "coordinator-owns-user-I/O" is enforced by **two** ADK mechanisms — neither alone is enough:
  1. **`disallow_transfer_to_parent=True`** on each specialist (cross-turn structural guarantee — the runner skips specialists when picking whose turn it is on the next user message).
  2. **`after_agent_callback`** that returns a synthetic `function_call` part — `Event.is_final_response()` returns False, so the LLM flow iterates again and the coordinator delivers the same-turn final reply.
- `disallow_transfer_to_peers=True` blocks specialist→specialist hops.
- Specialists' tool denylists are enforced **structurally** by simply not handing them write tools.
- A `ToolCallValidatorPlugin` catches "Tool not found" errors at runtime (e.g. when the model calls a tool filtered by plan mode) and returns a corrective response so the model self-corrects on the next iteration instead of aborting the turn.

## Layout

```
adk-cc/                           ← repo root
├── pyproject.toml                ← packages.find where=["agents"] → installs adk_cc
├── Dockerfile.sandbox            ← per-session sandbox image
├── .env.example                  ← config reference, GENERATED from adk_cc/config schema
├── docs/                         ← architecture, prompts, deployment runbooks
├── scripts/                      ← operator CLIs + skill/context/compaction demos
├── tests/                        ← unit tests + e2e suites (see "Tests")
├── web/                          ← React chat UI (Vite + Tailwind v4 + shadcn/ui)
│   └── src/{api,components,pages,lib}
└── agents/                       ← AGENTS_DIR (ADK discovers agents here, nothing else)
    └── adk_cc/                   ← agent package (imported as `adk_cc`)
        ├── __init__.py           ← .env bootstrap + `from . import agent`
        ├── agent.py              ← exposes `app` (preferred) and `root_agent`
        ├── prompts.py            ← per-agent instructions
        ├── logging_setup.py      ← ADK_CC_LOG_* configuration
        ├── config/               ← typed env-var schema (single source; gen/check/print)
        ├── tools/                ← AdkCcTool subclasses (read/write/exec/task/HITL/plan/skills/MCP)
        ├── plugins/              ← ADK BasePlugin integrations
        ├── permissions/          ← rule engine + confirmation payloads
        ├── sandbox/              ← SandboxBackend ABC + impls
        │   └── backends/{noop,docker,sandbox_service,daytona,e2b}.py
        ├── tasks/                ← task tracking + JSON storage (filelock-safe)
        ├── credentials/          ← per-tenant secret storage
        └── service/              ← FastAPI factory + auth + tenancy + admin
```

`agents/` is ADK's `AGENTS_DIR` — it holds only agent packages, so the loader discovers just the agents (not `web/`, `docs/`, `tests/`). The package installs and imports as top-level `adk_cc` (setuptools `where=["agents"]`); the uvicorn factory is `adk_cc.service.server:make_app`.

## Quick start

```bash
cd adk-cc
uv venv .venv && source .venv/bin/activate
uv pip install -e .

# .env file — at minimum the API key for your model server
echo 'ADK_CC_API_KEY=sk-your-model-server-key' > .env

# Option A: bundled ADK web UI (point it at the agents/ dir)
adk web agents

# Option B: one-shot CLI
adk run agents/adk_cc

# Option C: FastAPI + custom React UI (see "Web UI" below)

# Option D: native desktop app (single-user, no login; see "Desktop app" below)
#   npm --prefix web run build:desktop && cargo run --manifest-path src-tauri/Cargo.toml
```

## Web UI

A custom React chat lives in [`web/`](./web/). It replaces the bundled `adk web` UI for end-user chat with adk-cc-aware widgets: confirmation prompts, structured `ask_user_question` forms, plan/edit/bash artifact renderers, task sidebar, slash commands, theme, and SSE token streaming.

### Run it

```bash
# 1. Build the bundle (one-time, or whenever web/ changes)
npm --prefix web install
npm --prefix web run build

# 2. Start the FastAPI server with the UI mounted at /
ADK_CC_AGENTS_DIR=$(pwd)/agents \
ADK_CC_AUTH_TOKENS='devtok=alice:acme' \
ADK_CC_SERVE_UI=1 \
.venv/bin/uvicorn adk_cc.service.server:make_app --factory \
  --host 127.0.0.1 --port 8000

# 3. Open http://127.0.0.1:8000/ and sign in with token `devtok`
```

The `adk_cc` package auto-loads `.env` at import time (looks in
`ADK_CC_AGENTS_DIR`, then the repo root, then CWD), so `ADK_CC_API_KEY`
/ `ADK_CC_MODEL` / `ADK_CC_API_BASE` etc. from your `.env` reach
uvicorn without needing `set -a; . ./.env; set +a` first. Process env
always wins over `.env`. Set `ADK_CC_SKIP_DOTENV=1` to disable.

For HMR development against the same server use `npm --prefix web run dev` (Vite dev server on `:5173` proxies `/run*`, `/apps`, `/list-apps`, `/api`, `/admin`, `/debug` to `http://127.0.0.1:8000` by default; override via `ADK_CC_DEV_API`).

### What's in the UI

- **Session rail** — left nav with agent picker (`/list-apps`) + per-user session list + new/delete.
- **Thread** — flattens ADK events into chat rows. Streams tokens as they arrive (opt-in via `streaming: true` on `/run_sse`, with per-chunk delta accumulation).
- **Composer** — multi-line textarea. Enter sends, Shift+Enter newlines. Type `/` to open the slash-command picker.
- **adk-cc-aware widgets** (replace generic tool-call cards while pending):
  - `ConfirmationCard` — renders the `ConfirmPrompt` payload from the permission engine's "ask" branch (allow once / allow always / deny + optional comment + persist toggle). Triggered by `adk_request_confirmation` / `adk_cc_confirmation_form` function-calls.
  - `AskUserQuestionCard` — structured multi-choice / multi-select form. Triggered by `ask_user_question` long-running calls; auto-adds an "Other" free-form fallback.
- **Artifact renderers** (paired call+response in a single card):
  - `BashTerminalCard` (`run_bash`) — `$ command` prompt, stdout/stderr coloring, exit-code chip.
  - `FileEditCard` (`edit_file`, `write_file`) — side-by-side before/after for edits, single green block for writes.
  - `PlanCard` (`write_plan`, `read_current_plan`) — markdown content + storage path + collapsible history.
  - `HtmlArtifactPreview` (`.html` / `text/html` artifacts) — renders inline in a sandboxed `<iframe srcdoc>`. Default `sandbox=""` (no scripts) — static HTML/CSS shows, JS is inert, so JS-built content (e.g. Plotly) is blank. Opt in to interactive previews with the build-time flag `VITE_ADK_CC_HTML_PREVIEW_ALLOW_SCRIPTS=1`, which flips it to `sandbox="allow-scripts"` (runs untrusted JS, but `allow-same-origin` stays off so the frame can't reach your token/cookies/DOM). See [`.env.example`](./.env.example).
- **Task sidebar** — derives the live task list from `task_create` / `task_update` / `task_list` function-responses; no extra endpoint.
- **Plan mode** — when `session.state.permission_mode === "plan"`, the composer gets a violet badge + tinted border so the active mode is unambiguous at the moment of typing.
- **Slash commands** — `/help`, `/clear` (new session), `/plan` and `/exit-plan` (flip `permission_mode` directly via `PATCH /apps/.../sessions/{id}` with a `state_delta` — deterministic, no LLM round-trip), `/theme` (cycle light → dark → system), `/settings`, `/signout`.
- **Settings dialog** — lightweight modal (no Radix dep). Theme picker (light/dark/system), read-only identity rows, sign-out shortcut. Reachable via the gear icon or `/settings`.

### Auth + serving

The auth middleware exempts the SPA bundle paths (`/`, `/favicon.svg`, `/assets/*`) so the login form can load anonymously; everything else (`/run*`, `/apps/*`, `/list-apps`, `/debug/*`) stays gated. JWT (`JwtAuthExtractor`) and dev token map (`BearerTokenExtractor`) both work — the React app just posts a Bearer header.

UI-related env knobs:

```bash
ADK_CC_SERVE_UI=1                  # mount the SPA from web/dist at /
ADK_CC_UI_DIST=/path/to/web/dist   # override default (<repo>/web/dist)
ADK_CC_DEV_API=http://...:8000     # dev-only, for the Vite proxy
```

## Desktop app (Tauri)

A native desktop build wraps the same backend + React UI as a **single-user
local app** (Tauri v2 / Rust). One window, one bundled backend, **no login**.
The UI is the **same shared components** as the web app behind a thin desktop
shell, selected at build time by `VITE_ADK_CC_DESKTOP=1` — the default (no-flag)
build is still today's web app, so the web side is unchanged.

What's different from the web app:

- **Projects = local git directories.** The sidebar is two levels: **L1
  projects → L2 that project's sessions**. "Add" picks a folder (a non-repo
  folder is `git init`-ed on add).
- **Each session runs in its own git worktree** of the project repo (branch
  `adk-cc/<session-id>`), so parallel sessions are isolated working copies.
- **Single-user, no auth**; per-project history under a local data dir.

### Prerequisites

- Rust toolchain (`cargo`) — first build compiles the Tauri deps (minutes).
- Node + the web deps: `npm --prefix web install`.
- The repo `.venv` with the Python backend installed (same as the web app).
- A `.env` with your model key/endpoint (`ADK_CC_API_KEY` / `ADK_CC_MODEL` /
  `ADK_CC_API_BASE`) — the sidecar runs with the repo as its CWD and loads it.

### Run it (dev)

```bash
# 1. Build the desktop UI bundle (the backend sidecar serves web/dist-desktop)
npm --prefix web run build:desktop

# 2. Build + launch the app (opens the window, spawns the backend on :8765)
cargo run --manifest-path src-tauri/Cargo.toml
```

One-liner if you have the Tauri CLI (`cargo install tauri-cli`): `cargo tauri
dev` — it runs `build:desktop` for you via `beforeDevCommand`, then launches.

The shell (`src-tauri/src/main.rs`) spawns
`.venv/bin/uvicorn adk_cc.service.server:make_app --factory --port 8765` from
the repo with the single-user env baked in — `ADK_CC_ALLOW_NO_AUTH=1`,
`ADK_CC_DESKTOP=1`, `ADK_CC_TENANCY_MODE=single`, sqlite sessions,
encrypted-file secrets, `noop` sandbox, `ADK_CC_SERVE_UI=1` from
`web/dist-desktop` — polls `/list-apps`, points the window at
`http://127.0.0.1:8765/`, and kills the backend on exit.

Frontend scripts: `build:desktop` (production bundle → `web/dist-desktop`),
`dev:desktop` (Vite dev server with the desktop shell, for UI iteration).

### Data + distribution

Per-user data lives under **`~/.adk-cc-desktop`**: `sessions.db`,
`credential.key` (+ `secrets/`), `projects.json`, and
`worktrees/<project>/<session>`. Delete it to reset.

`cargo tauri build` produces a bundle, but v1 **runs the repo `.venv`** (the
compile-time repo path) — a frozen/bundled backend and signed installers are a
follow-up, as are the session diff/merge UI (v1 isolates in worktrees and shows
the branch name) and per-project settings beyond Appearance.

## Local model

Uses LiteLLM under ADK's `LiteLlm` wrapper, pointed at an OpenAI-compatible server.

**Defaults**:
- model: `openai/Qwen3.6-35B-A3B-UD-MLX-4bit`
- api base: `http://localhost:18000/v1`
- api key: read from `ADK_CC_API_KEY` (required)

Override any of these via env without code changes:

```bash
ADK_CC_MODEL=openai/<model-id>          # e.g. openai/qwen2.5-coder-32b
ADK_CC_API_BASE=http://host:port/v1
ADK_CC_API_KEY=<token>
```

Pick a model with **function-calling support** — this loop relies on tool use. Qwen 2.5+, Llama 3.1/3.2, and Mistral families all work. Small (1B–3B) models often handle tool calls poorly.

See [`.env.example`](./.env.example) for the full configuration surface. It is
**generated** from the typed schema in [`agents/adk_cc/config/`](./agents/adk_cc/config/),
the single source of truth for every `ADK_CC_*` var (tier, default, help, allowed
values), so the docs can't drift from the code. Use the schema CLI to regenerate,
validate, or inspect a deployment:

```bash
python -m adk_cc.config gen-env --out .env.example   # regenerate this file
python -m adk_cc.config check                        # validate the current env
python -m adk_cc.config print                        # effective values (secrets masked)
```

The server also runs `check` at boot (web and desktop), logging any missing-required,
out-of-range-enum, or dangerous/contradictory combinations so misconfiguration
surfaces loudly instead of failing silently later.

## Sandbox backends

All host-touching tools (`run_bash`, `read_file`, `write_file`, `edit_file`, skill scripts) route through a `SandboxBackend`. Pick one with `ADK_CC_SANDBOX_BACKEND`:

| Backend | When to use | Isolation |
|---|---|---|
| `noop` (default) | `adk web .` dev on your laptop | None — runs on the host. Safety guard refuses prod-shaped paths unless `ADK_CC_NOOP_ACK_HOST_EXEC=1` |
| `docker` | Single-instance production with your own Docker daemon | Per-session container, read-only rootfs, dropped caps, mem/cpu/pids limits, `network_mode=none` by default |
| `sandbox_service` | When you'd rather not give the agent process Docker daemon access | Delegates to an external REST sandbox service ([JISUlicious/sandboxing](https://github.com/JISUlicious/sandboxing) or compatible) — gVisor + cap-drop + read-only rootfs + userns-remap + Squid egress allowlist |
| `e2b` | (stub) | Future: hosted Firecracker microVMs |

For `docker` and `sandbox_service`, see [`docs/04-deployment-sandbox.md`](./docs/04-deployment-sandbox.md) for the operator setup. The `sandbox_service` path is the lighter-touch option (no Docker daemon on your agent host); the `docker` path keeps everything in-process.

### Streaming exec (operator-side observability)

Set `ADK_CC_BASH_STREAM=1` to log `run_bash` chunks at INFO as they arrive instead of waiting for the command to finish. The model still receives one aggregated final result; the streaming is for the operator tailing the agent log. Currently only `sandbox_service` actually streams chunks live; `noop` / `docker` use the ABC default (one chunk at end). See `adk_cc/sandbox/backends/base.py` for the `exec_stream()` contract.

## Skills

Skills are operator-defined parameterized prompts (Anthropic skill format). adk-cc auto-loads skills from `adk_cc/skills/` (or `ADK_CC_SKILLS_DIR`) at boot and exposes them via four model-callable tools: `list_skills`, `load_skill`, `load_skill_resource`, `run_skill_script`.

Skill scripts execute through the same `SandboxBackend` as `run_bash` — so a skill's Python script runs inside the per-session container on `docker` / `sandbox_service`, and on the host (sandbox-bypassed) only on `noop`.

```bash
ADK_CC_SKILLS_DIR=/path/to/skill-folders   # default: adk_cc/skills/ if it exists
```

A non-canonical skill layout (e.g. doc files at the skill root, not under `references/`) is handled by a fallback `load_skill_resource` that does a filesystem scan when ADK's strict path lookup misses.

Project-level skills are also auto-discovered from `.adk-cc/skills/` and `.claude/skills/` in the project's working tree (disable via `ADK_CC_DISABLE_PROJECT_SKILLS=1`).

## Project context

`ProjectContextPlugin` auto-loads project-level context files into every agent invocation's `system_instruction`. Resolution order:

1. `.adk-cc/CONTEXT.md` (project-owned)
2. `CLAUDE.md` (Claude Code convention)
3. `AGENTS.md` (Agent.md convention)

The first match wins. Disable via `ADK_CC_DISABLE_PROJECT_CONTEXT=1`. Override the search list via `ADK_CC_CONTEXT_FILES=path1,path2`.

## MCP servers

Attach external MCP servers to the coordinator. Three ways, by scale — all expose their tools as `mcp__<server_name>__*` so permission rules can target a server (e.g. deny `mcp__github__*`):

**One static server (dev / quick start)** — env vars:

```bash
ADK_CC_MCP_SERVER="python tests/fixtures/csv_mcp_server.py"  # stdio command, or URL for sse/http
ADK_CC_MCP_SERVER_NAME=csv          # tool prefix → mcp__csv__*
ADK_CC_MCP_TRANSPORT=stdio          # stdio | sse | http
```

**Multiple static servers (single-tenant, several servers)** — a JSON file, same for every user:

```bash
ADK_CC_MCP_SERVERS_FILE=/etc/adk-cc/mcp.json
```
```json
[
  {"server_name": "github", "transport": "http", "url": "https://api.github.com/mcp",
   "credential_key": "GITHUB_MCP_TOKEN", "tool_filter": ["list_repos", "create_issue"]},
  {"server_name": "csv", "transport": "stdio", "url": "python tests/fixtures/csv_mcp_server.py"}
]
```

A JSON array of `McpServerConfig` objects, loaded at boot and merged with the single `ADK_CC_MCP_SERVER` above (a duplicate `server_name` is dropped with a warning). Fault-isolated: a bad file, or one bad entry, is logged and skipped — boot is never blocked. For a server needing auth, `credential_key` names an **env var** holding the bearer token (no credential store exists at boot); a missing var wires the server unauthenticated with a warning.

**Per-tenant servers (multi-tenant, different per user)** — the registry. Set `ADK_CC_TENANT_REGISTRY_DIR` and store one `mcp.json` per tenant. The `TenantMcpToolset` resolves servers per-invocation from the active tenant's config (hot-reloaded), credentials substitute from the credential provider, and an admin HTTP API can add/remove servers at runtime. For single-tenant deployments you can use one tenant_id (defaults to `"local"` in dev).

See [`.env.example`](./.env.example) for every MCP / registry / credential env var, and `docs/05-production-deployment.md` for the tenant admin API.

## Admin panel

A built-in admin UI to manage **MCP servers, skills, and model endpoints at runtime** — no restart, no editing files on the box. Default-OFF; enable with `ADK_CC_ADMIN_PANEL=1`:

```bash
ADK_CC_ADMIN_PANEL=1 \
ADK_CC_SERVE_UI=1 ADK_CC_UI_DIST=$(pwd)/web/dist \
ADK_CC_AUTH_TOKENS='admintok=alice:local:admin' \
ADK_CC_AGENTS_DIR=$(pwd)/agents \
.venv/bin/uvicorn adk_cc.service.server:make_app --factory --port 8000
# open http://127.0.0.1:8000/admin  (sign in with a token whose principal holds the admin role)
```

- **What it manages** — three tabs: **MCP servers** (add/edit/delete; per-server transport, tool filter, credential key), **Skills** (upload a `.zip` with a `SKILL.md`, delete), **Model endpoints** (add backends and **activate** one to switch the live model — the agent picks it up on the next request via a `SelectableLlm` that resolves the active endpoint per call).
- **Global mode** — manages one deployment-wide config set, pinned to `ADK_CC_GLOBAL_TENANT_ID` (default `local`). It rides the per-tenant registry machinery (hot-reloaded per invocation), so edits take effect live.
- **Auth** — the `/admin` page loads anonymously (it's the React shell); every admin **API** call is gated on the admin role (`ADK_CC_ADMIN_ROLE`, default `admin`), from the JWT roles claim or the dev token's `:roles` segment. Secrets are never returned over HTTP (credential + model-endpoint keys are referenced by env-var name; lists show key *names* only).

See [`.env.example`](./.env.example) (`ADK_CC_ADMIN_PANEL`, `ADK_CC_ADMIN_ROLE`, `ADK_CC_GLOBAL_TENANT_ID`, `ADK_CC_ADMIN_DATA_DIR`, `ADK_CC_MODEL_REGISTRY_FILE`) and `docs/05-production-deployment.md` for the route reference.

## Single-instance server deployment

`adk web .` is great for dev. For a long-running single-instance server (e.g. a dev VM, a trusted internal team, a one-tenant deployment), use the FastAPI factory:

```bash
uvicorn adk_cc.service.server:make_app --factory --host 0.0.0.0 --port 8000
```

`make_app` reads everything from env (see [`.env.example`](./.env.example)) and adds the production-only plugin chain `[Audit, Tenancy, Permission, Quota, PlanModeReminder, TaskReminder, ToolCallValidator, ContextGuard]` on top of whatever the agent's `App` already registers (`adk_cc/agent.py` adds `ProjectContext`, `AskUserQuestionUiHint`, `ConfirmationFormUi`, `ModelIOTrace`), plus an auth middleware. `SessionRetryPlugin` ships in `adk_cc/plugins/` but is opt-in — wire it explicitly if you need stale-session recovery. The factory **fails closed on auth**: refuses to start unless one of these is set:

- `ADK_CC_AUTH_TOKENS=tok1=alice:tenant_a,tok2=bob:tenant_b` — static token map (single-instance, simple)
- `ADK_CC_JWT_JWKS_URL=...` + `ADK_CC_JWT_ISSUER=...` etc. — JWT validation
- `ADK_CC_ALLOW_NO_AUTH=1` — explicit dev escape (don't use in production)

### Minimal single-tenant production-ish recipe

For "I just want one server, one team, persistent sessions, real isolation":

```bash
# .env (or process env vars)
ADK_CC_API_KEY=sk-your-model-key
ADK_CC_MODEL=openai/<your-model>
ADK_CC_API_BASE=http://your-model-server:18000/v1

# Static token map: everyone is in tenant=internal
ADK_CC_AUTH_TOKENS=alice_token=alice:internal,bob_token=bob:internal

# Sandbox: pick one
ADK_CC_SANDBOX_BACKEND=docker
ADK_CC_DOCKER_HOST=unix:///var/run/docker.sock        # local Docker
# OR
ADK_CC_SANDBOX_BACKEND=sandbox_service
ADK_CC_SANDBOX_SERVICE_URL=http://localhost:8000      # JISUlicious/sandboxing
ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN=<bearer>

# Per-user persistent workspaces under <root>/<tenant>/<user>/
ADK_CC_WORKSPACE_ROOT=/var/lib/adk-cc/wks

# Persistent sessions (sqlite is enough for one-instance)
ADK_CC_SESSION_DSN=sqlite:////var/lib/adk-cc/sessions.db

# Audit log
ADK_CC_AUDIT_LOG=/var/log/adk-cc/audit.jsonl

# Permissions YAML (optional but recommended)
ADK_CC_PERMISSIONS_YAML=/etc/adk-cc/permissions.yaml

# Custom UI (optional — bundles the React chat at /)
ADK_CC_SERVE_UI=1
```

Then:

```bash
uvicorn adk_cc.service.server:make_app --factory --host 0.0.0.0 --port 8000
```

That's a fully working single-instance deployment with:
- Real sandbox isolation per session
- Per-user persistent workspaces (alice's files survive her next session)
- Persistent sessions (the conversation history survives a process restart)
- Static-token auth with one tenant
- Audit log of every tool call
- The custom React chat UI mounted at `/`

For multi-tenant deployments and the full readiness checklist (security / reliability / observability / ops gaps), see [`docs/05-production-deployment.md`](./docs/05-production-deployment.md).

## Tenancy

`tenant_id` is read from auth (JWT claim or static token map). The `TenancyPlugin` lazy-seeds session state on the first tool call:

```
state["temp:tenant_context"]    →  TenantContext(tenant_id, user_id, root)
state["temp:sandbox_workspace"] →  WorkspaceRoot(<root>/<tenant>/<user>/)
state["temp:sandbox_backend"]   →  per-session backend instance
```

The default resolver bridges the auth-extracted tenant_id into the plugin layer via a ContextVar. So `ADK_CC_AUTH_TOKENS=tok=alice:acme` actually scopes alice into `tenant=acme` — no custom resolver needed. Operators with bespoke `user_id → tenant_id` mapping logic can supply a `tenant_resolver=callable` to `TenancyPlugin`.

`adk web .` always runs as `tenant_id="local"` (no auth = single-tenant dev).

See [`docs/02-architecture.md`](./docs/02-architecture.md) §7.6 (workspace layout) for how persistence works per (tenant, user, session).

## Plan mode

When the work warrants a written plan with user approval before any change, the coordinator calls `enter_plan_mode(reason=...)` and the session enters plan mode (`permission_mode="plan"`). `PlanModeReminderPlugin` then:

- Filters write/exec tools (`write_file`, `edit_file`, `run_bash`, `task_create`, `task_update`, `enter_plan_mode`) out of the LLM's tool surface.
- Keeps read tools, `write_plan` / `read_current_plan` / `exit_plan_mode` / `ask_user_question`, and the `Explore` sub-agent visible.
- Injects a planning instruction (4-step process: understand → explore → design → detail; required output format for `write_plan`).

The coordinator produces the plan via `write_plan` (each call creates a new timestamped file under `<workspace>/.adk-cc/plans/`) and ends its turn with `exit_plan_mode`, which prompts the user for explicit approval. Approval flips `permission_mode` back and write tools reappear.

Plan mode is asymmetric with `exit_plan_mode`: entering tightens posture (no confirmation), exiting relaxes (user must approve).

The web UI also exposes `/plan` and `/exit-plan` slash commands that flip `permission_mode` directly via `PATCH /apps/.../sessions/{id}` with a `state_delta` — deterministic and skips the LLM round-trip.

## Confirmations

Destructive tool calls (and any ASK-rule match) pause for human confirmation via ADK's `request_confirmation` seam. `PermissionPlugin` sends a structured `ConfirmPrompt` payload with three options — **Allow once / Allow always / Deny**. "Allow always" injects a SESSION-scope ALLOW rule keyed by `(tool, extracted rule key)` so the same operation isn't re-asked for the rest of the session; scope is intentionally narrow (exact rule-key match, no wildcards).

`ConfirmationFormUiPlugin` (registered by default) bridges this to the bundled `adk web` UI so the options actually render as a selectable form instead of a hardcoded binary checkbox. The custom React UI in `web/` reads the structured payload directly (`ConfirmationCard.tsx`) and posts back the `chose_id` / `comment` / `persist_across_sessions` response — no rewrite plugin needed for that path.

Wire contract: [`docs/06-confirmation-protocol.md`](./docs/06-confirmation-protocol.md).

## Tasks

Four `task_*` tools (`task_create` / `task_get` / `task_list` / `task_update`) for pure tracking — no execution semantics. Tasks persist as JSON files under `<workspace>/.adk-cc/tasks/<session_id>/<task_id>.json` (override the root via `ADK_CC_TASKS_DIR`). Tasks survive process restarts; multi-worker deployments are safe via `filelock` writes.

Three statuses: `pending`, `in_progress`, `completed`. Task tools are filtered out in plan mode (tasks are an act-time progress checklist, not a planning surface).

A `TaskReminderPlugin` injects the active task list into the model's context periodically — fires when the model has gone too many turns without using `task_create`/`task_update` (default 10) and at least that many turns have passed since the last reminder (default 10):

```bash
ADK_CC_TASK_REMINDER_TURNS_SINCE_WRITE=10
ADK_CC_TASK_REMINDER_TURNS_BETWEEN=10
```

The web UI surfaces the live task list as a right-rail `TaskSidebar` derived from `task_*` function-responses in the session event log.

## Tests

```
tests/
├── test_*.py                          ← unit tests (mocked I/O, fast; ~126 checks)
│   ├── test_sandbox_service_backend.py
│   ├── test_workspace_layout.py
│   ├── test_skill_resource_fallback.py
│   ├── test_session_retry.py
│   ├── test_context_guard.py
│   ├── test_tenancy_resolver.py
│   ├── test_permissions_confirmation.py
│   ├── test_ask_user_question_ui_hint.py
│   ├── test_confirmation_form_ui.py
│   ├── test_plan_mode_env_default.py
│   ├── test_plan_mode_tools_env_default.py
│   ├── test_read_file_limits.py
│   ├── test_token_counter.py
│   ├── test_logging_setup.py
│   ├── test_model_io_trace.py
│   ├── test_audit_extensions.py
│   ├── test_project_context.py
│   └── test_compaction_audit.py
│
├── e2e_features.py                    ← in-process FastAPI e2e (auth + admin + skill upload)
├── e2e_confirmation_flow.py           ← in-process ADK Runner e2e — confirmation gate, allow_always session rule, deny path, scope-narrow check
├── e2e_confirmation_form_ui.py        ← in-process ADK Runner e2e — sentinel-name rewrite, form-shaped resume, deny path with form widget
├── e2e_ask_user_question.py           ← in-process ADK Runner e2e — long_running pause, no premature response event, resume with user answer
│
└── e2e against a live sandbox service:
    ├── e2e_sandbox_service.py         9 contract checks + 6 bug-fix verifications
    ├── e2e_sandbox_comprehensive.py   53 checks across 9 categories
    ├── e2e_skills.py                  6 — full skill chain
    ├── e2e_streaming_adapter.py       9 — exec_stream + BashTool stream
    └── diag_streaming_timing.py       diagnostic (always-on probe)
```

Run unit tests with no env config required:

```bash
.venv/bin/python tests/test_sandbox_service_backend.py
.venv/bin/python tests/test_workspace_layout.py
# ... etc.
```

Run e2e tests against a live sandbox service (point at a running JISUlicious/sandboxing instance):

```bash
ADK_CC_SANDBOX_SERVICE_URL=http://127.0.0.1:8000 \
SANDBOX_API_TOKEN=<token> \
  .venv/bin/python tests/e2e_sandbox_comprehensive.py
```

Both `e2e_skills.py` and `e2e_streaming_adapter.py` need Python 3.12+ and adk-cc importable; they preflight reachability and skip cleanly if not.

## Status

**Alpha** (`v0.0.1` is the first tagged release). Functional and exercised end-to-end by 28 test files (20 unit + 8 e2e/diagnostic), with a working React chat UI shipping on `feat/chat-ui`. Has known operational gaps documented in [`docs/05-production-deployment.md`](./docs/05-production-deployment.md)'s readiness checklist (security / reliability / observability / ops / multi-tenancy / config / tests). Close the ✗ items appropriate to your threat model and SLO before serving real users.
