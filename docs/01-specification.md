# Specification

## Purpose

`adk-cc` is a **coordinator + specialists** agent loop on Google ADK 1.31.1, modeled after Claude Code's gather → plan → act → verify discipline. It runs as a single agent module loadable by `adk web` or `adk run` for development, and as a FastAPI service via `adk_cc.service.server:make_app` for multi-tenant deployment.

## Surface

- **Discovery**: `adk web` / `adk run` finds the agent via the module-level export `adk_cc.agent.app` (preferred) or `adk_cc.agent.root_agent`. The directory layout matches ADK's documented convention: `<AGENTS_DIR>/<agent_name>/{__init__.py, agent.py}`.
- **Entry points**:
  - Development: `adk web .` or `adk run adk_cc` from the `adk-cc/` directory.
  - Production: `uvicorn adk_cc.service.server:make_app --factory` (multi-tenant, with auth, quotas, postgres-backed sessions).
- **Configuration**: env-driven, loaded from `adk_cc/.env` automatically in dev. Required minimum: `ADK_CC_API_KEY`. The full surface is documented in [`.env.example`](../.env.example) — model, permissions, sandbox backend, audit, web fetch, skills, tasks, and the multi-tenant service variables.

## Roles

There are three agents:

| Agent | Role | Tools |
|---|---|---|
| `coordinator` (root) | Owns user I/O. Decides which step is next. Acts directly on simple tasks; delegates on complex ones. Becomes a planning agent when it calls `enter_plan_mode` (tool surface narrows; planning instruction injected). | All read tools, write/exec tools, task tools, plan-mode tools (`enter_plan_mode`, `exit_plan_mode`, `write_plan`, `read_current_plan`), `ask_user_question`, `web_fetch`, plus auto-loaded skills and MCP toolsets |
| `Explore` | Read-only codebase searcher. Returns a written report. | `read_file`, `glob_files`, `grep`, `web_fetch` |
| `verification` | Adversarial verifier. Runs builds/tests/probes. Ends with a parsed `VERDICT: PASS\|FAIL\|PARTIAL` line. | `read_file`, `glob_files`, `grep`, `run_bash`, `web_fetch`, `read_current_plan` (with prompt-enforced `/tmp`-only writes) |

Planning is **not** a sub-agent. The coordinator handles planning itself by entering plan mode (see [02-architecture.md §3.5](./02-architecture.md#35-plan-mode-as-coordinator-posture)).

## Behavior contract

**User-facing I/O is owned by the coordinator.** Specialists never address the user — their reports are visible in the event stream (and the `adk web` UI) for transparency, but the final user-facing reply always comes from the coordinator. This is enforced by two ADK mechanisms (see [02-architecture.md §3](./02-architecture.md#3-coordinator-owns-user-io-dual-mechanism)).

**Gather → plan → act → verify is a discipline, not a state machine.** The runtime does not sequence the steps; the coordinator decides per turn what's next. Sequencing is steered by the coordinator's prompt:

- **Gather**: directed lookups via `read_file` / `glob_files` / `grep`; broad exploration via `transfer_to_agent(agent_name='Explore')`.
- **Plan**: the coordinator calls `enter_plan_mode` when the work warrants a written plan with user approval. Inside plan mode it produces a plan via `write_plan` and ends with `exit_plan_mode`. Skip for trivial work.
- **Act**: `write_file`, `edit_file`, `run_bash`. Risky actions (destructive ops, force-pushes, shared-state changes) require user confirmation per the prompt.
- **Track**: `task_create` / `task_update` / `task_list` / `task_get` for ACT-time progress visibility on multi-step work. Filtered out in plan mode.
- **Verify**: required for non-trivial implementation (3+ file edits, backend/API, infrastructure). Coordinator owns the gate; the verifier's verdict is parsed and the coordinator must spot-check.

## Constraints

- **Specialists cannot mutate the project.** They have no `write_file`, `edit_file`, or full `run_bash` (verification has `run_bash` but its prompt restricts writes to `/tmp`). Enforced by simply not handing those tools to the specialist.
- **Specialists cannot recurse or hop sideways.** Each has `disallow_transfer_to_peers=True`; none of them list `AgentTool` in their tool surface.
- **Specialists cannot become the active agent for a future user message.** Each has `disallow_transfer_to_parent=True`; ADK's runner consequently routes the next turn back to the coordinator (see [02-architecture.md §3.1](./02-architecture.md#31-cross-turn-disallow_transfer_to_parenttrue)).
- **Verification verdict is a parsed contract.** The verifier's prompt requires a literal `VERDICT: PASS|FAIL|PARTIAL` line; the coordinator's prompt requires it to act on that line and spot-check on PASS.
- **Plan-mode tool surface is filtered at the LLM layer.** `PlanModeReminderPlugin.before_model_callback` removes write/exec/task tools from `llm_request.tools_dict` and the function-declaration list when `permission_mode == "plan"`. The model can't call what it doesn't see; `ToolCallValidatorPlugin` catches any hallucinated calls as a safety net.

## Out of scope (deferred or pluggable, not implemented)

- A custom CLI or web UI (uses `adk web` / `adk run` for dev; FastAPI factory for prod).
- E2B / Kubernetes / Modal / nsjail sandbox backends (the `SandboxBackend` ABC is the seam; only `NoopBackend` and `DockerBackend` are implemented today, plus a stub for `E2BBackend`).
- Per-host outbound network filtering inside `run_bash` (today: all-or-nothing; per-domain filtering needs a sidecar proxy).
- A real `transfer_to_agent` handler for the synthetic `_handback_to_coordinator` call (it's a control signal only — see [02-architecture.md §3.2](./02-architecture.md#32-same-turn-after_agent_callback)).
