# Specification

## Purpose

`adk-cc` is a minimal **coordinator + specialists** agent loop on Google ADK 1.31.1, modeled after Claude Code's gather → plan → act → verify discipline. It runs as a single agent module loadable by `adk web` or `adk run`.

## Surface

- **Discovery**: `adk web` / `adk run` finds the agent via the module-level export `adk_cc.agent.root_agent`. The directory layout matches ADK's documented convention: `<AGENTS_DIR>/<agent_name>/{__init__.py, agent.py}`.
- **Entry point**: there is no CLI, no FastAPI server, no custom runner. The user invokes `adk web .` (or `adk run adk_cc`) from the `adk-cc/` directory.
- **Configuration**: three environment variables, loaded from `adk_cc/.env` automatically:
  - `ADK_CC_API_KEY` (required) — bearer token for the model server.
  - `ADK_CC_API_BASE` (optional, default `http://localhost:18000/v1`) — OpenAI-compatible endpoint.
  - `ADK_CC_MODEL` (optional, default `openai/Qwen3.6-35B-A3B-UD-MLX-4bit`) — LiteLLM model id.

## Roles

There are four agents:

| Agent | Role | Tools |
|---|---|---|
| `coordinator` (root) | Owns user I/O. Decides which step is next. Acts directly on simple tasks; delegates on complex ones. | `read_file`, `glob_files`, `grep`, `write_file`, `edit_file`, `run_bash` |
| `Explore` | Read-only codebase searcher. Returns a written report. | `read_file`, `glob_files`, `grep` |
| `Plan` | Read-only software architect. Returns an implementation strategy + critical files list. | `read_file`, `glob_files`, `grep` |
| `verification` | Adversarial verifier. Runs builds/tests/probes. Ends with a parsed `VERDICT: PASS\|FAIL\|PARTIAL` line. | `read_file`, `glob_files`, `grep`, `run_bash` (with prompt-enforced `/tmp`-only writes) |

## Behavior contract

**User-facing I/O is owned by the coordinator.** Specialists never address the user — their reports are visible in the event stream (and the `adk web` UI) for transparency, but the final user-facing reply always comes from the coordinator. This is enforced by two ADK mechanisms (see [02-architecture.md §3](./02-architecture.md#3-coordinator-owns-user-io-dual-mechanism)).

**Gather → plan → act → verify is a discipline, not a state machine.** The runtime does not sequence the steps; the coordinator decides per turn what's next. Sequencing is steered by the coordinator's prompt:

- **Gather**: directed lookups via `read_file` / `glob_files` / `grep`; broad exploration via `transfer_to_agent(agent_name='Explore')`.
- **Plan**: required for multi-step or architectural changes; skip for trivial work.
- **Act**: `write_file`, `edit_file`, `run_bash`. Risky actions (destructive ops, force-pushes, shared-state changes) require user confirmation per the prompt.
- **Verify**: required for non-trivial implementation (3+ file edits, backend/API, infrastructure). Coordinator owns the gate; the verifier's verdict is parsed and the coordinator must spot-check.

## Constraints

- **Specialists cannot mutate the project.** They have no `write_file`, `edit_file`, or full `run_bash` (verification has `run_bash` but its prompt restricts writes to `/tmp`). Enforced by simply not handing those tools to the specialist.
- **Specialists cannot recurse or hop sideways.** Each has `disallow_transfer_to_peers=True`; none of them list `AgentTool` in their tool surface.
- **Specialists cannot become the active agent for a future user message.** Each has `disallow_transfer_to_parent=True`; ADK's runner consequently routes the next turn back to the coordinator (see [02-architecture.md §3.1](./02-architecture.md#31-cross-turn-disallow_transfer_to_parenttrue)).
- **Verification verdict is a parsed contract.** The verifier's prompt requires a literal `VERDICT: PASS|FAIL|PARTIAL` line; the coordinator's prompt requires it to act on that line and spot-check on PASS.

## Out of scope

- A custom CLI or web UI (uses `adk web` / `adk run` as-is).
- Persistence or session restoration beyond what ADK's `InMemorySessionService` provides.
- Multi-user routing.
- Sandbox/permission engine equivalents (relies on the model and tool access boundaries — no equivalent of Claude Code's permission engine or hooks system).
- A real `transfer_to_agent` handler for the synthetic `_handback_to_coordinator` call (it's a control signal only — see [02-architecture.md §3.2](./02-architecture.md#32-same-turn-after_agent_callback)).
