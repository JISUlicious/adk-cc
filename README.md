# adk-cc

A minimal Claude-Code-style **gather → plan → act → verify** agent loop, implemented as a single ADK agent module loadable by `adk web` / `adk run`.

Detailed docs live in [`docs/`](./docs/): [specification](./docs/01-specification.md), [architecture](./docs/02-architecture.md), [prompts](./docs/03-prompts.md), [sandbox runbook](./docs/04-deployment-sandbox.md). The TL;DR:

- One **coordinator** ("main agent") is the ONLY agent that talks to the user. Acts directly with read tools (`read_file`, `glob_files`, `grep`, `web_fetch`, `read_current_plan`), write/exec tools (`write_file`, `edit_file`, `run_bash`), task tools (`task_create` / `task_get` / `task_list` / `task_update`), HITL tools (`ask_user_question`), plan-mode tools (`enter_plan_mode`, `exit_plan_mode`, `write_plan`), and any auto-loaded skills/MCP toolsets.
- Two specialists wired as ADK `sub_agents`: **`Explore`** (broad codebase search returning a written report) and **`verification`** (adversarial post-implementation gate ending with a parsed `VERDICT: PASS|FAIL|PARTIAL` line). Delegation is `transfer_to_agent` — and because sub-agents share the parent's invocation context, all their tool calls and responses stream into `adk web` (not buried inside an opaque tool result like `AgentTool` would do).
- **Planning is not a sub-agent.** When the user wants a written plan with approval before any change, the coordinator calls `enter_plan_mode` — a posture the coordinator takes. `PlanModeReminderPlugin` then dynamically filters write/exec tools out of the LLM's tool surface and injects a planning instruction. The coordinator persists the plan via `write_plan` and ends its turn with `exit_plan_mode` to gate re-entry to the unrestricted surface on user approval.
- Hub-and-spoke + "coordinator-owns-user-I/O" is enforced by **two** ADK mechanisms — neither alone is enough:
  1. **`disallow_transfer_to_parent=True`** on each specialist. ADK's `runner._find_agent_to_run` only picks an agent whose `_is_transferable_across_agent_tree()` is True, which requires `disallow_transfer_to_parent=False` on the agent and all ancestors. Setting it `True` makes the runner skip the specialist when picking whose turn it is on the next user message → next turn always lands on the coordinator. **Hard structural guarantee for cross-turn routing.**
  2. **`after_agent_callback`** that returns `Content` containing a synthetic `Part(function_call=...)`. `Event.is_final_response()` returns False when the event has function calls, so `base_llm_flow.run_async`'s `while True` loop iterates again → another coordinator LLM step → coordinator synthesizes the user-facing reply from the specialist's report in history. **Forces coordinator to deliver the same-turn final reply.**
- `disallow_transfer_to_peers=True` blocks specialist→specialist hops.
- Specialists' tool denylists are enforced **structurally** by simply not handing them write tools.
- The coordinator's instruction is the **routing table** — it names each specialist by `agent.name`.
- The verifier's contract is enforced **socially** via its prompt + the parsed `VERDICT: PASS|FAIL|PARTIAL` line the coordinator's prompt tells it to look for.
- A `ToolCallValidatorPlugin` catches ADK's "Tool not found" errors at runtime (e.g. when the model calls a tool filtered by plan mode) and returns a corrective tool response so the model self-corrects on the next iteration instead of aborting the turn.

## Layout

```
adk-cc/                  ← AGENTS_DIR
├── pyproject.toml
├── Dockerfile.sandbox   ← per-session sandbox image (Stage C)
├── docs/
└── adk_cc/              ← agent subdirectory (the "agent_name" adk web expects)
    ├── __init__.py      ← `from . import agent`
    ├── agent.py         ← exposes `app` (preferred) and `root_agent`
    ├── prompts.py       ← per-agent instructions
    ├── tools/           ← AdkCcTool subclasses
    ├── plugins/         ← ADK BasePlugin integrations
    ├── permissions/     ← rule engine (Stage B)
    ├── sandbox/         ← SandboxBackend ABC + impls (Stage C)
    ├── tasks/           ← task tracking + storage (Stage F)
    └── service/         ← FastAPI factory for multi-tenant deployment (Stage G)
```

## Run

```bash
cd adk-cc
uv venv .venv && source .venv/bin/activate
uv pip install -e .

# point adk web at this directory (the parent of adk_cc/)
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

Put the auth key in `adk_cc/.env` — `adk web` and `adk run` auto-load it:

```bash
# adk_cc/.env
ADK_CC_API_KEY=sk-your-key
```

**Override** any of these via env without code changes:

```bash
ADK_CC_MODEL=openai/<model-id>
ADK_CC_API_BASE=http://host:port/v1
ADK_CC_API_KEY=<token>
```

Pick a model with **function-calling support** — this loop relies on tool use. Qwen 2.5+, Llama 3.1/3.2, and Mistral families all work. Small (1B–3B) models often handle tool calls poorly.

See [`.env.example`](./.env.example) for the full configuration surface (sandbox, permissions, audit, web fetch, skills, tasks, multi-tenant).

## Production deployment

`adk web .` is fine for dev. For multi-tenant deployment, use the FastAPI factory in `adk_cc.service`:

```bash
export ADK_CC_AGENTS_DIR=/path/to/parent/of/adk_cc
export ADK_CC_SESSION_DSN=postgresql://user:pass@host/db
export ADK_CC_PERMISSIONS_YAML=/etc/adk-cc/permissions.yaml
export ADK_CC_PERMISSION_MODE=default     # plan|acceptEdits|bypassPermissions|dontAsk
export ADK_CC_QUOTA_PER_MINUTE=120
export ADK_CC_SANDBOX_BACKEND=docker      # see docs/04-deployment-sandbox.md
export ADK_CC_AUTH_TOKENS="tok=user:tenant"  # dev only; replace with real JWT validator
export ADK_CC_API_KEY=...                  # for the model server

uvicorn adk_cc.service.server:make_app --factory --host 0.0.0.0 --port 8000
```

The factory wires the full plugin chain (`[Audit, Tenancy, Permission, Quota, PlanModeReminder, TaskReminder, ToolCallValidator]`), the configured session backend, and an auth middleware. See [`docs/02-architecture.md`](./docs/02-architecture.md) for the topology and [`docs/04-deployment-sandbox.md`](./docs/04-deployment-sandbox.md) for the sandbox host setup.

`make_app()` **fails closed on auth**: if neither `ADK_CC_AUTH_TOKENS` (the dev `BearerTokenExtractor`) nor `ADK_CC_ALLOW_NO_AUTH=1` (explicit dev escape) is set, it refuses to start. Real production deployments should implement an `AuthExtractor` and call `build_fastapi_app(auth_extractor=...)` from a custom factory rather than going through `make_app()`.

## Plan mode

When the work warrants a written plan with user approval before any change is made, the coordinator calls `enter_plan_mode(reason=...)` and the session enters plan mode (`permission_mode = "plan"`). The `PlanModeReminderPlugin` then:

- Filters write/exec tools (`write_file`, `edit_file`, `run_bash`, `task_create`, `task_update`, `enter_plan_mode`) out of the LLM's tool surface.
- Keeps read tools, `write_plan` / `read_current_plan` / `exit_plan_mode` / `ask_user_question`, and the `Explore` sub-agent visible.
- Injects a planning instruction (4-step process: understand → explore → design → detail; required output format for `write_plan`).

The coordinator produces the plan via `write_plan` (each call creates a new timestamped file under `<workspace>/.adk-cc/plans/`) and ends its turn with `exit_plan_mode`, which prompts the user for explicit approval. On approval, `permission_mode` flips back and write tools reappear.

Plan mode is asymmetric with `exit_plan_mode`: entering tightens posture, so it requires no confirmation; exiting relaxes posture, so the user must approve.

## Tasks

Four `task_*` tools (`task_create` / `task_get` / `task_list` / `task_update`) for pure tracking — no execution semantics. Tasks persist as JSON files at `~/.adk-cc/tasks/<tenant_id>/<session_id>/<task_id>.json` (override the root via `ADK_CC_TASKS_DIR`). Tasks survive process restarts; multi-worker deployments are safe via `filelock` writes. Layout and shape mirror upstream Claude Code's v2 task family (`src/utils/tasks.ts`).

Three statuses: `pending`, `in_progress`, `completed`. Task tools are filtered out in plan mode (tasks are an ACT-time progress checklist, not a planning surface).

A `TaskReminderPlugin` injects the active task list into the model's context periodically — fires when the model has gone too many turns without using `task_create`/`task_update` (default 10) and at least that many turns have passed since the last reminder (default 10). Override either threshold via env:

```bash
ADK_CC_TASK_REMINDER_TURNS_SINCE_WRITE=10
ADK_CC_TASK_REMINDER_TURNS_BETWEEN=10
```

Mirrors upstream's `task_reminder` attachment pattern (`attachments.ts:3395-3432` + `messages.ts:3680-3699`); the reminder text is identical, scoped to adk-cc's snake_case tool names.
