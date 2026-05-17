# Architecture

This is the longer write-up of the data-science branch. The
[README](../README.md) covers what it is and how to run it; this
document covers why it's shaped the way it is.

## The loop

```
∅ ─load_from_*──▶ explore ─list/describe/peek/profile──▶ plan
                                                          │
                                              record_plan │
                                                          ▼
       done ◀──verify_completion(PASS)── verify ◀─count==len(plan)── act
```

Four stages, one terminal (`done`), all tracked in
`tool_context.state["temp:loop_stage"]` by `StageGuardPlugin`. The
plugin does two things and only two things:

1. **`before_model_callback`** — read the current stage, prepend a
   `<stage-nudge stage=...>` block to `llm_request.config.system_instruction`
   telling the model what's expected next.
2. **`after_tool_callback`** — advance the stage based on what just
   ran:
   - Any tool with `cls.stage == "explore"` while stage was None →
     `explore`. Second explore tool while already in `explore` →
     `plan` (a hint forward).
   - `record_plan` → `act` (regardless of prior stage, including
     re-plans).
   - Any tool with `cls.stage == "act"` AND
     `len(temp:loop_results) >= len(temp:loop_plan)` → `verify`.
   - `verify_completion` returning `verdict: PASS` → `done`.

No hard gates. The discipline is advisory; the actual
PASS/FAIL decision lives in `verify_completion`'s body, which
combines a deterministic rule check (plan recorded, results count ≥
plan length, conclusion non-empty) with the critic's structured
verdict. Both must agree for PASS.

## Coordinator vs specialists

The coordinator (root agent) is the only agent the user ever sees.
It owns three loop-control tools:

- `record_plan(steps: list[str])` — write an ordered list of steps
  to `temp:loop_plan`. Each step should correspond to ONE
  specialist dispatch.
- `read_plan()` — return the current plan. The model uses this when
  it forgets where it is.
- `verify_completion(user_query, conclusion, critic_verdict)` — the
  final gate.

Everything else is in sub-agents. Five of them:

| Sub-agent | Owns | Stage tag on its tools |
|---|---|---|
| `loader` | `load_from_registry`, `load_from_db_mock`, `load_from_file_mock` | `explore` |
| `explorer` | `list_datasets`, `describe_dataset`, `peek_dataset`, `profile_dataset` | `explore` |
| `processor` | `filter_dataset`, `aggregate_dataset`, `correlate`, `drop_na`, `transform_column`, `select_columns` | `act` |
| `visualizer` | `render_bar_chart`, `render_table`, `summarize_distribution` | `act` |
| `critic` | (no tools — output_schema instead) | `verify` |

Each specialist has:

- `disallow_transfer_to_parent=True` so when its turn ends the runner
  defaults back to the coordinator.
- `disallow_transfer_to_peers=True` so a specialist can't reach
  another specialist directly.
- `after_agent_callback=force_coordinator_continuation` (in
  `sub_agents/_shared.py`) that returns a synthetic `function_call`
  Content. `Event.is_final_response()` returns False on a
  function_call event, so `base_llm_flow.run_async` keeps its
  while-loop alive and the coordinator gets one more LLM turn to
  read the specialist's report and decide what's next.

The coordinator transfers to a specialist via ADK's built-in
`transfer_to_agent(agent_name=...)`. Specialists run their tool(s)
and end their turn with a brief textual summary; the handback
callback fires; the coordinator picks back up.

## The critic gate

Self-judgment by the coordinator (the original `llm_judgment` arg on
`verify_completion`, since removed) has near-zero independence — a
model grading its own work in the same context has every incentive
to say "looks good". The `critic` sub-agent replaces that with an
adversarial pass:

- Runs in a fresh invocation context with its own model
  configuration (env-driven via `ADK_CC_CRITIC_*`).
- Has NO tools. Reads the user's original message + every prior tool
  call from the session events ADK passes in. The coordinator
  cannot hide anything from it.
- Emits ONLY a JSON object matching `CriticVerdict` (the schema in
  `sub_agents/critic/schema.py`):

```json
{
  "verdict": "PASS" | "FAIL" | "PARTIAL",
  "addressed_aspects": [...],
  "missing_aspects": [...],
  "evidence_quality": "strong" | "weak" | "insufficient",
  "reasoning": "..."
}
```

ADK enforces the shape via the agent's `output_schema=CriticVerdict`.

The coordinator reads the critic's JSON from the conversation
history and passes the whole object verbatim as the
`critic_verdict` arg of `verify_completion`. The tool's body:

1. Rule check: plan recorded, `len(results) >= len(plan)`,
   conclusion non-empty.
2. Critic check: `critic_verdict.verdict == "PASS"`.
3. Final verdict: PASS iff BOTH pass.

A `field_validator(mode="before")` on `critic_verdict` tolerates
the common failure mode where weaker function-callers emit nested
objects as JSON-encoded strings (observed on
`stepfun-ai/step-3.5-flash` and `minimaxai/minimax-m2.7`).

### Critic-FAIL recovery

When the critic returns `verdict: FAIL` or `PARTIAL` with non-empty
`missing_aspects`, the coordinator prompt's recovery instructions
direct it to:

1. Read `missing_aspects`.
2. Dispatch to a specialist that can address each gap.
3. Update the draft conclusion.
4. Re-transfer to `critic` for re-evaluation.

Capped at 3 critique cycles per run. The `verify → act` backward
stage transition fires when `record_plan` is re-called from inside
the verify stage; the audit trail captures it as the signature of
the recovery path. See
`tests/test_data_science_agent_recovery.py` for an e2e demo.

## Plugin chain

In order (chain order matters for early-return semantics, see
`google/adk/plugins/plugin_manager.py:_run_callbacks`):

1. **`AuditPlugin`** — always first. Observes every
   `before_tool_callback`, `after_tool_callback`, and
   `on_tool_error_callback`; writes JSONL via `_emit`. Returns
   `None` from every callback, so it never blocks the chain.
2. **`StageGuardPlugin`** — `before_model_callback` (nudge) +
   `after_tool_callback` (transitions). Returns `None` always.
3. **`ToolCallValidatorPlugin`** — `on_tool_error_callback`. Catches
   the specific `ValueError("Tool ... not found.\nAvailable tools:
   ...")` ADK raises when a model hallucinates a tool name, and
   returns a structured `function_response` that the model can read
   and self-correct from. Returns `None` for any other error.
4. **`ContextGuardPlugin`** — `before_model_callback`. Pre-flights
   the prompt token count using `permissions/token_counter.py`'s
   estimator. WARN at 75% of `ADK_CC_MAX_CONTEXT_TOKENS`, REJECT at
   95% (returns a synthetic `LlmResponse`). No-op when the env var
   isn't set.
5. **`ModelIOTracePlugin`** — `before_model_callback` and
   `after_model_callback`. Off unless `ADK_CC_LOG_MODEL_IO=1`. When
   on, dumps each LlmRequest / LlmResponse as a `model_request` /
   `model_response` audit event with the payload truncated at
   `ADK_CC_LOG_MODEL_IO_MAX_BYTES` (default 50KB).

Plus `plugins/session_retry.py`, which is imported for its
side-effect (monkey-patches `SqliteSessionService.append_event` /
`DatabaseSessionService.append_event` when
`ADK_CC_SESSION_RETRY_ON_STALE=1`). Not in the plugin chain; pure
service-level patch.

## Tool framework

`adk_cc/tools/base.py` defines `AdkCcTool` (subclass of ADK's
`BaseTool`) and `ToolMeta`. Each tool declares:

- `meta: ClassVar[ToolMeta]` — `name`, `is_read_only`,
  `is_concurrency_safe`, `long_running`. (Earlier revisions had
  `is_destructive`, `needs_sandbox`, `requires_user_approval` —
  removed when the permission engine + sandbox layer were dropped.)
- `input_model: ClassVar[type[BaseModel]]` — Pydantic input schema.
- `description: ClassVar[str]` — what ADK sends to the model.
- `async def _execute(self, args, ctx) -> dict` — the body.

`AdkCcTool.run_async` validates `args` against `input_model` before
calling `_execute`. A validation failure returns a structured
`{"status": "input_validation_error", "errors": [...]}` that the
model can read on its next turn rather than aborting the run.

`long_running=True` triggers `tool_context.actions.skip_summarization
= True` after `_execute` returns, which keeps the parent flow loop
alive while the asynchronous response is awaited.

## State keys

All loop-related state goes under the `temp:` prefix so it doesn't
persist into ADK's durable session record:

- `temp:loop_stage` — current stage (`explore` / `plan` / `act` /
  `verify` / `done` / None at start)
- `temp:loop_plan` — `list[str]` of step descriptions
- `temp:loop_results` — `list[dict]` of acting-tool results (one
  entry per `aggregate_dataset` / `filter_dataset` / etc. call, via
  `tools/loop_state.py:stash_result`)
- `temp:datasets_loaded` — `list[dict]` of `{ts, source, name,
  row_count}` records appended by the loader's tools via
  `tools/loop_state.py:record_load`

## Audit JSONL schema

One JSON object per line in `~/.adk-cc/audit.jsonl` (override via
`ADK_CC_AUDIT_LOG`). Common fields on every event: `ts` (float
Unix seconds), `event` (string from the set below), `agent_name`,
`session_id`, `invocation_id`, `function_call_id`, `user_id`.

| Event | Source | Extra fields |
|---|---|---|
| `tool_call_attempt` | `AuditPlugin.before_tool_callback` | `tool_name`, `tool_meta`, `tool_args` |
| `tool_call_result` | `AuditPlugin.after_tool_callback` | `tool_name`, `tool_meta`, `tool_args`, `result_status` |
| `tool_call_error` | `AuditPlugin.on_tool_error_callback` | `tool_name`, `tool_meta`, `tool_args`, `error_type`, `error_message` |
| `loop_stage_transition` | `StageGuardPlugin.after_tool_callback` | `from`, `to`, `trigger_tool` |
| `model_request` | `ModelIOTracePlugin.before_model_callback` (opt-in) | `model`, `tool_count`, `content_turns`, `payload_bytes`, `payload` (truncated), `truncated` |
| `model_response` | `ModelIOTracePlugin.after_model_callback` (opt-in) | `parts_count`, `error_code`, `error_message`, `payload_bytes`, `payload`, `truncated` |

Append-only. No rotation. Operators are expected to ship this to a
real log sink (Datadog, Loki, S3) in production.

## What was removed

This branch is a deliberate fork of `main` with ~5.6k LoC deleted.
The deletions trace to one observation: the data-science variant
has no destructive tools, so the elaborate infrastructure built
around destructive-tool gating is dead weight. Removed:

- **Permission engine** (`permissions/engine.py`, `rules.py`,
  `settings.py`, `broadening.py`, `confirmation.py`,
  `modes.py`) — no `is_destructive` tools to gate.
- **PermissionPlugin** — every decision trivialized to "allow";
  AuditPlugin already recorded every call.
- **Sandbox layer** (`sandbox/` with 4 backends + workspace +
  config + code_executor) — no tools needed sandboxed execution.
- **Credentials provider** — only the sandbox-service backend used
  it.
- **Service tier** (`service/auth.py`, `service/tenancy.py`,
  `service/registry.py`, `service/server.py`) — `adk api_server`
  loads the agent directly from `agent.py`; this scaffolding was
  never invoked on the demo path.
- **Task storage** (`tasks/`) — the `task_create` / `task_update`
  tools were deleted earlier; this was the backend.
- **ProjectContextPlugin** — filesystem walk dead-ish on cloud
  deploys where cwd is a container scratch dir.
- **YAML permission loader** (`config/`).

Git history on this branch's parents has everything if needed.
