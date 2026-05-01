# adk-cc

A minimal Claude-Code-style **gather → act → verify** agent loop, implemented as a single ADK agent module loadable by `adk web` / `adk run`.

Detailed docs live in [`docs/`](./docs/): [specification](./docs/01-specification.md), [architecture](./docs/02-architecture.md), [prompts](./docs/03-prompts.md). The TL;DR:

- One **coordinator** ("main agent") is the ONLY agent that talks to the user. Acts directly with `read_file`, `glob_files`, `grep`, `write_file`, `edit_file`, `run_bash`.
- Three specialists wired as ADK `sub_agents`: `Explore` (gather), `Plan`, `verification`. Delegation is `transfer_to_agent` — and because sub-agents share the parent's invocation context, all their tool calls and responses stream into `adk web` (not buried inside an opaque tool result like `AgentTool` would do).
- Hub-and-spoke + "coordinator-owns-user-I/O" is enforced by **two** ADK mechanisms — neither alone is enough:
  1. **`disallow_transfer_to_parent=True`** on each specialist. ADK's `runner._find_agent_to_run` only picks an agent whose `_is_transferable_across_agent_tree()` is True, which requires `disallow_transfer_to_parent=False` on the agent and all ancestors. Setting it `True` makes the runner skip the specialist when picking whose turn it is on the next user message → next turn always lands on the coordinator. **Hard structural guarantee for cross-turn routing.**
  2. **`after_agent_callback`** that returns `Content` containing a synthetic `Part(function_call=...)`. `Event.is_final_response()` returns False when the event has function calls, so `base_llm_flow.run_async`'s `while True` loop iterates again → another coordinator LLM step → coordinator synthesizes the user-facing reply from the specialist's report in history. **Forces coordinator to deliver the same-turn final reply.**
- `disallow_transfer_to_peers=True` blocks specialist→specialist hops.
- Specialists' tool denylists are enforced **structurally** by simply not handing them write tools.
- The coordinator's instruction is the **routing table** — it names each specialist by `agent.name`.
- The verifier's contract is enforced **socially** via its prompt + the parsed `VERDICT: PASS|FAIL|PARTIAL` line the coordinator's prompt tells it to look for.

## Layout

```
adk-cc/                 ← AGENTS_DIR
├── pyproject.toml
└── adk_cc/             ← agent subdirectory (the "agent_name" adk web expects)
    ├── __init__.py     ← `from . import agent`
    ├── agent.py        ← exposes `root_agent`
    ├── prompts.py      ← per-agent instructions
    └── tools.py        ← read/write/exec tools
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

## Production deployment

`adk web .` is fine for dev. For multi-tenant deployment, use the FastAPI factory in `adk_cc.service`:

```bash
export ADK_CC_AGENTS_DIR=/path/to/parent/of/adk_cc
export ADK_CC_SESSION_DSN=postgresql://user:pass@host/db
export ADK_CC_PERMISSIONS_YAML=/etc/adk-cc/permissions.yaml
export ADK_CC_PERMISSION_MODE=default     # plan|acceptEdits|bypassPermissions|dontAsk
export ADK_CC_QUOTA_PER_MINUTE=120
export ADK_CC_SANDBOX_BACKEND=docker      # or e2b (operator implements)
export ADK_CC_AUTH_TOKENS="tok=user:tenant"  # dev only; replace with real JWT validator
export ADK_CC_API_KEY=...                  # for the model server

uvicorn adk_cc.service.server:make_app --factory --host 0.0.0.0 --port 8000
```

The factory wires the full plugin chain (`[Audit, Tenancy, Permission, Quota, PlanModeReminder, TaskReminder]`), the configured session backend, and an auth middleware. See `docs/02-architecture.md` for the topology.

## Tasks

Four `task_*` tools (`task_create` / `task_get` / `task_list` / `task_update`) for pure tracking — no execution semantics. Tasks persist as JSON files at `~/.adk-cc/tasks/<tenant_id>/<session_id>/<task_id>.json` (override the root via `ADK_CC_TASKS_DIR`). Tasks survive process restarts; multi-worker deployments are safe via `filelock` writes. Layout and shape mirror upstream Claude Code's v2 task family (`src/utils/tasks.ts`).

A `TaskReminderPlugin` injects the active task list into the model's context periodically — fires when the model has gone too many turns without using `task_create`/`task_update` (default 10) and at least that many turns have passed since the last reminder (default 10). Override either threshold via env:

```bash
ADK_CC_TASK_REMINDER_TURNS_SINCE_WRITE=10
ADK_CC_TASK_REMINDER_TURNS_BETWEEN=10
```

Mirrors upstream's `task_reminder` attachment pattern (`attachments.ts:3395-3432` + `messages.ts:3680-3699`); the reminder text is identical, scoped to adk-cc's snake_case tool names.
