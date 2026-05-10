# adk-cc

A minimal Claude-Code-style **gather → plan → act → verify** agent loop, implemented as a single ADK agent module loadable by `adk web` / `adk run`, plus an opt-in FastAPI factory for single-instance server deployment.

Deeper docs in [`docs/`](./docs/): [specification](./docs/01-specification.md), [architecture](./docs/02-architecture.md), [prompts](./docs/03-prompts.md), [sandbox runbook](./docs/04-deployment-sandbox.md), [production runbook](./docs/05-production-deployment.md).

## What it is

- One **coordinator** (the only agent that talks to the user). Acts directly with read tools (`read_file`, `glob_files`, `grep`, `web_fetch`, `read_current_plan`), write/exec tools (`write_file`, `edit_file`, `run_bash`), task tools (`task_create` / `task_get` / `task_list` / `task_update`), HITL tools (`ask_user_question`), plan-mode tools (`enter_plan_mode`, `exit_plan_mode`, `write_plan`), and any auto-loaded skills/MCP toolsets.
- Two specialists wired as ADK `sub_agents`: **`Explore`** (broad codebase search returning a written report) and **`verification`** (adversarial post-implementation gate ending with `VERDICT: PASS|FAIL|PARTIAL`). Delegation is `transfer_to_agent`; sub-agents share the parent's invocation context, so all tool calls/responses stream into `adk web` rather than being buried in an opaque tool result.
- **Planning is not a sub-agent.** When the user wants a written plan with approval before any change, the coordinator calls `enter_plan_mode` — a posture the coordinator takes. `PlanModeReminderPlugin` then dynamically filters write/exec tools out of the LLM's tool surface and injects a planning instruction. The plan persists via `write_plan`; `exit_plan_mode` gates re-entry to the unrestricted surface on user approval.
- Hub-and-spoke + "coordinator-owns-user-I/O" is enforced by **two** ADK mechanisms — neither alone is enough:
  1. **`disallow_transfer_to_parent=True`** on each specialist (cross-turn structural guarantee — the runner skips specialists when picking whose turn it is on the next user message).
  2. **`after_agent_callback`** that returns a synthetic `function_call` part — `Event.is_final_response()` returns False, so the LLM flow iterates again and the coordinator delivers the same-turn final reply.
- `disallow_transfer_to_peers=True` blocks specialist→specialist hops.
- Specialists' tool denylists are enforced **structurally** by simply not handing them write tools.
- A `ToolCallValidatorPlugin` catches "Tool not found" errors at runtime (e.g. when the model calls a tool filtered by plan mode) and returns a corrective response so the model self-corrects on the next iteration instead of aborting the turn.

## Layout

```
adk-cc/                           ← AGENTS_DIR (parent of adk_cc/)
├── pyproject.toml
├── Dockerfile.sandbox            ← per-session sandbox image (Stage C)
├── docs/                         ← architecture, prompts, deployment runbooks
├── scripts/
│   ├── scratch_reaper.py         ← cron-style cleanup of session scratch dirs
│   └── sandbox_destroy.py        ← operator CLI for sandbox session teardown
├── tests/                        ← unit tests + e2e suites (see "Tests" below)
└── adk_cc/                       ← agent subdirectory
    ├── __init__.py               ← `from . import agent`
    ├── agent.py                  ← exposes `app` (preferred) and `root_agent`
    ├── prompts.py                ← per-agent instructions
    ├── tools/                    ← AdkCcTool subclasses
    ├── plugins/                  ← ADK BasePlugin integrations
    ├── permissions/              ← rule engine (Stage B)
    ├── sandbox/                  ← SandboxBackend ABC + impls (Stage C)
    │   ├── backends/
    │   │   ├── noop_backend.py            ← host execution, dev only
    │   │   ├── docker_backend.py          ← per-session container, prod-grade
    │   │   ├── sandbox_service_backend.py ← REST client for an external
    │   │   │                                 sandbox service (gVisor isolation)
    │   │   └── e2b_backend.py             ← stub (microVM, future)
    │   └── code_executor.py      ← BaseCodeExecutor adapter for skill scripts
    ├── tasks/                    ← task tracking + storage (Stage F)
    ├── credentials/              ← per-tenant secret storage
    └── service/                  ← FastAPI factory + auth + tenancy
```

## Quick start

```bash
cd adk-cc
uv venv .venv && source .venv/bin/activate
uv pip install -e .

# .env file — the API key for your model server
echo 'ADK_CC_API_KEY=sk-your-model-server-key' > .env

# Point adk web at this directory (the parent of adk_cc/)
adk web .
# or one-shot:
adk run adk_cc
```

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

See [`.env.example`](./.env.example) for the full configuration surface.

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

## MCP servers

Connect external MCP servers via the per-tenant registry. Set `ADK_CC_TENANT_REGISTRY_DIR` and store one `mcp.json` per tenant. The `TenantMcpToolset` resolves servers per-invocation from the active tenant's config; credentials substitute from the credential provider.

For single-tenant deployments, you can use one tenant_id (defaults to `"local"` in dev). See [`.env.example`](./.env.example) for the registry / credential env vars.

## Single-instance server deployment

`adk web .` is great for dev. For a long-running single-instance server (e.g. a dev VM, a trusted internal team, a one-tenant deployment), use the FastAPI factory:

```bash
uvicorn adk_cc.service.server:make_app --factory --host 0.0.0.0 --port 8000
```

`make_app` reads everything from env (see [`.env.example`](./.env.example)) and wires the full plugin chain — `[Audit, Tenancy, Permission, Quota, PlanModeReminder, TaskReminder, ToolCallValidator]` — plus an auth middleware. It **fails closed on auth**: refuses to start unless one of these is set:

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

## Tasks

Four `task_*` tools (`task_create` / `task_get` / `task_list` / `task_update`) for pure tracking — no execution semantics. Tasks persist as JSON files under `<workspace>/.adk-cc/tasks/<session_id>/<task_id>.json` (override the root via `ADK_CC_TASKS_DIR`). Tasks survive process restarts; multi-worker deployments are safe via `filelock` writes.

Three statuses: `pending`, `in_progress`, `completed`. Task tools are filtered out in plan mode (tasks are an act-time progress checklist, not a planning surface).

A `TaskReminderPlugin` injects the active task list into the model's context periodically — fires when the model has gone too many turns without using `task_create`/`task_update` (default 10) and at least that many turns have passed since the last reminder (default 10):

```bash
ADK_CC_TASK_REMINDER_TURNS_SINCE_WRITE=10
ADK_CC_TASK_REMINDER_TURNS_BETWEEN=10
```

## Tests

```
tests/
├── test_*.py                       ← unit tests (mocked I/O, fast)
│   ├── test_sandbox_service_backend.py   24 tests
│   ├── test_workspace_layout.py          11 tests
│   ├── test_skill_resource_fallback.py    6 tests
│   ├── test_session_retry.py              7 tests
│   ├── test_context_guard.py             10 tests
│   └── test_tenancy_resolver.py           7 tests   ── 65 unit tests total
│
├── e2e_features.py                 ← in-process FastAPI e2e (auth + admin + skill upload)
│
└── e2e against a live sandbox service:
    ├── e2e_sandbox_service.py            9 contract checks + 6 bug-fix verifications
    ├── e2e_sandbox_comprehensive.py     53 checks across 9 categories
    ├── e2e_skills.py                     6 — full skill chain
    ├── e2e_streaming_adapter.py          9 — exec_stream + BashTool stream
    └── diag_streaming_timing.py          diagnostic (always-on probe)
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

**Alpha.** Functional and exercised end-to-end (~135 unit + e2e checks across 11 test files). Has known operational gaps documented in [`docs/05-production-deployment.md`](./docs/05-production-deployment.md)'s readiness checklist (security / reliability / observability / ops / multi-tenancy / config / tests). Close the ✗ items appropriate to your threat model and SLO before serving real users.
