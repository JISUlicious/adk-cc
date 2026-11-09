# adk-cc → Google ADK 2.x (≥ 2.14) migration: analysis and specification

Date: 2026-11-09. Target: `google-adk>=2.14,<3` (2.14.0 released 2026-11-04). Current pin: `google-adk==1.31.1` (2026-04-21).

Sources, in order of authority: (1) the 2.14.0 wheel diffed against the installed 1.31.1 (signature-level, this document); (2) the ADK repo's unit guides `docs/guides/**/index.md` (runner, abort, events, apps/resumability, apps/events_compaction, workflow/base_node); (3) `CHANGELOG.md` 2.0.0→2.14.0 (breaking + behavior sections); (4) the `analysis-docs` session's gap check `google-adk/migration/문서격차점검_v1.26.0_v2.14.0.md` (flows/plugins/sessions/skills — code-verified) and release-ops note. Items the analysis-docs session did **not** verify (graph runtime, event schema, resumability, LiteLlm) were verified here against the wheel and guides. Everything marked **verify** below is still an inference.

---

## 1. Where adk-cc stands (as-is, 2026-11-09)

- 787 commits on `main`; 211 Python files / 53.5K lines under `agents/adk_cc`; 80 files import `google.adk`. Python 3.12 (2.x requires ≥3.11 — fine).
- Architecture: one `LlmAgent` coordinator (`agent.py`) with specialists reached by `transfer_to_agent`; **32 plugins** (8.9K lines) carrying most product behavior (permissions, confirmation UI, compaction defense, memory, notes, titles, mid-turn messages, microcompact, verify nudge…); **43 tools** (8.9K lines) incl. the skills subsystem (`tools/skills.py`, ~2.6K lines: discovery, trust gate, materialization, launcher wrapper, deps install, gates); **sandbox** layer (20 files, 7.9K lines: noop/container/docker/ssh/daytona/e2b/sandbox-service backends, process registry, analysis env); **service** layer (25 files, 9.4K lines): `get_fast_api_app` wrapped by `server.py`, Turn Broker (`turns.py`) for durable runs, `FileSessionService` (JSONL), admin/desktop/dataset/process routes; memory + wiki + identity + authz; desktop (Tauri) and web shells.
- Hook usage: `before_model` ×15, `before_tool` ×7, `after_model` ×6, `after_tool` ×5, `on_event` ×3, `on_user_message` ×2, `on_tool_error` ×2, `before_run` ×2, `after_run` ×2.
- Test corpus: 307 files in `tests/`; 23 drive a real `Runner`/`InMemoryRunner` with scripted models; 27 are live (`ADK_CC_LIVE=1`).
- **Private-API touchpoints (the migration's real surface)** — everything else is public API that still exists in 2.14:

| Where | What | 2.14 status |
|---|---|---|
| `service/turns.py` `install_confirmation_resume_fix` | monkeypatches `Runner._resolve_invocation_id` to force a confirmation answer into a NEW invocation (ADK 1.31 yielded zero events when resuming an ended gated invocation) | method still exists but was **rewritten** (resolves across all responses via `_resolve_invocation_id_from_fr`); 2.12 changed resume routing for non-resumable apps. Re-measure, likely remove. |
| `service/turns.py` `extract_adk_web_server` | scans route closures for the `AdkWebServer` instance to share its services with the broker | `AdkWebServer` **deprecated**; server split into `cli/api_server.py` + `cli/dev_server.py`. Rework. |
| `tools/skills.py` | replaces `skill_toolset._SkillScriptCodeExecutor` with `_WiderScriptCodeExecutor`; reads `toolset._tools`, `_get_skill`, `_project_scoped` | class still exists, `__init__` shape changed; whole `RunSkillScriptTool` contract changed (breaking in 2.14). Rebuild. |
| `plugins/tolerant_tool_json.py` | shims `lite_llm.json` and wraps `_message_to_generate_content_response` | function signature unchanged; upstream added JSON-tolerant parsing (2.12) and malformed-call reporting (2.10). Re-validate, probably remove. |
| `plugins/session_retry.py` | wraps `append_event` on the sqlite/database session services and retries on a `ValueError` whose text matches "stale session"/"modified in storage" | 2.14 raises `StaleSessionError(ValueError)` (`errors/_stale_session_error.py`) and the Runner itself tolerates it in post-invocation compaction. Re-target to the exception type or drop. |
| plugins/tools | `callback_context._invocation_context` (9 sites) | attribute still present. Keep, centralize. |
| `agent.py` | `agent._invocation_context`; a comment documents `_find_agent_to_run`'s routing, no code depends on it | routing moved into `agents/_agent_router.py`; nothing to change. |
| `plugins/handback_hygiene.py` | `_force_coordinator_continuation` (ours) | n/a |
| direct event writes | `session_service.append_event` from `midturn_messages`, `session_title`, `turns.py` (orphan prune, interrupted-call seal) | allowed by the service, but 2.x's rule is "yield events from the node; do not append or enqueue directly". See §4. |

---

## 2. What changed in ADK 2.x that matters here (verified)

### 2.1 Runtime
- **Graph runtime**: new `google.adk.workflow` package (`BaseNode`, `Node`, `FunctionNode`, `JoinNode`, `Workflow`, `NodeRunner`, `RetryConfig`, `NodeInterruptedError`, `NodeTimeoutError`, `RequestInput`, `OpenInterrupt`). `BaseAgent(BaseNode, abc.ABC)`; `BaseNode.run()` is `@final`; custom execution goes in `run_node_impl` (for `Node`) or in callbacks. **Overrides of `_run_async_impl` are silently ignored** — adk-cc has none.
- **`LlmAgent` is still flow-driven**: `_llm_flow` → `SingleFlow`/`AutoFlow`; the `SingleFlow` request-processor list has 12 entries in both versions (plus 2 response processors; verified in `single_flow.py`), **order changed** — the only move is `_interactions` before `_contents`: basic→auth→confirmation→instructions→identity→compaction→interactions→contents→cache→planning→code_execution→schema. New in 2.14: a second list, `BaseLlmFlow.tool_request_processors` (`_toolset_auth` → `_agent_tools` → `_dynamic_instructions`), runs *after* the first, so a 2.14 request passes **15** processors. The split carries a rule (`base_llm_flow.py` `__init__` comment): `llm_request.tools_dict` is empty throughout `request_processors` and is populated from `_agent_tools` on — a custom processor that needs the resolved tools must go in the second list, never be appended to the first (1.x code that appended to the single list lands in a "no tools yet" slot on 2.x). Public by-name helpers `get/replace/insert_before/insert_after/remove_request_processor` search both lists and reject duplicate names — the sanctioned extension point instead of reaching into flow internals. adk-cc touches flow processors today (agents/adk_cc/tools/ask_user_question.py) — audit that site against this rule in P1. Flow modules moved behind shims (`contents` → `context/_contents`, `instructions` → `prompt/_instructions`, …); old import paths work, old *internals* don't (`_get_contents` is gone from `contents.py`).
- **`LlmAgent.mode`** (`chat` | `task` | `single_turn`): sub-agents default `chat`, workflow nodes `single_turn`. `AgentTool` gained in-context variants that run via `ctx.run_node` instead of an isolated Runner; default `AgentTool` is unchanged. Parent context-cache config propagates to children (TTL ≤ 300 s).
- **Sequential/Parallel/Loop agents are deprecated** (README, 2.13) in favor of `Workflow`. adk-cc uses none.

### 2.2 Events and sessions
- The `events/` package grew from 3 files to 14 (`_abort_events`, `_interrupts`, `_isolation_scope`, `_node_path_builder`, `_internal_metadata`, `_branch_path`, …); old `node_path` maps to `node_info.path` for backward compatibility. `Event` gains `output: Any | None`, `output_for`, `node_info: NodeInfo` (`path`, derived `name`/`run_id`/`parent_run_id`), `isolation_scope` (internal, Task API), convenience ctor kwargs (`message`, `state`, `route`, `node_path`). camelCase aliases; serialize with `by_alias=True`. Framework-populated — do not set `node_info` manually.
- `BaseSessionService.append_event`: applies temp state, trims temp delta, `_commit_event_to_session` (state + `session.events.append`); may raise `StaleSessionError` in persistent impls; new `flush()` (no-op default) and `get_user_state()`. Abstract methods unchanged (`create_session`, `get_session`, `list_sessions`, `delete_session`).
- Rule from the 2.0 page: *"Do not append events directly to the session, and do not use `enqueue_event` directly. Yield the event from the node or agent so the framework handles persistence, routing, and streaming."*
- Storage: PostgreSQL JSON columns now `json` (2.14); Firestore format not readable by ≤2.13 after rollback; `sessions/migration/` helpers for sqlalchemy pickle/sqlite. `GetSessionConfig(num_recent_events, after_timestamp)` via `RunConfig.get_session_config`.
- **adk-cc impact**: `FileSessionService` stores `event.model_dump(mode="json")` and reads with `Event.model_validate` → the new fields round-trip without a schema change (**verify** with a 2.14 round-trip test, including `node_info` and `isolation_scope`). The web client's `RunEvent` type carries an index signature, so unknown fields are tolerated.

### 2.3 Runner and App
- `Runner(app= | agent= | node=, app_name=, plugins=, …, auto_create_session=False)`; `run_async(*, user_id, session_id, invocation_id=None, new_message=None, state_delta=None, run_config=None, yield_user_message=False, abort_signal: asyncio.Event | None=None)`.
- **Abort**: cooperative `abort_signal` attached to the InvocationContext; on abort the Runner runs `_synthesize_abort_events_if_needed` → `events/_abort_events.py::_build_abort_events`: every dangling function call of the aborted invocation gets a synthetic `function_response` `{'error': 'Invocation was aborted by client.'}`, grouped per (author, branch, isolation_scope), with `error_code=INVOCATION_ABORTED`; if nothing is dangling, one content-less abort event authored by the root agent. The events go through `_process_and_append_event`, so plugins' `on_event` see them; sealing is skipped when the invocation's last event is a paused-task reply and runs at most once per invocation. **Difference from adk-cc's hand-written `_close_interrupted_calls`**: upstream seals long-running calls *including confirmation and credential requests*; ours deliberately leaves `adk_request_confirmation`/`adk_cc_confirmation_form` open so the user can still answer after Stop. P2 must decide which semantics the UI keeps.
- **Compaction moved into the Runner** (`_run_post_invocation_compaction`, after `after_run_callback`); token-threshold compaction also runs before each model call; a plugin's `before_run_callback` can replace the invocation's `events_compaction_config` (per-session compaction, 2.14); ranges never end between a tool call and its response — `_event_resolved_response_ids` excludes ids listed in `requested_tool_confirmations`/`requested_auth_configs`, and `_provably_dead_call_ids` protects HITL/confirmation calls of the newest invocation (verified in `apps/compaction.py`; this is the #119 class of bug, now handled upstream). Post-invocation compaction is skipped for aborted invocations and discards its summary on `StaleSessionError`. 2.14 counts tool outputs since the last model call, so the same `token_threshold` fires earlier.
- `ResumabilityConfig` / `EventsCompactionConfig` live in `apps/_configs.py` (re-exported from `apps.app` and `apps` — verified); field names unchanged (`compaction_interval`, `overlap_size`, `token_threshold`, `event_retention_size`, `summarizer`).
- Resumability: `is_resumable=True` is app-wide; a FunctionResponse resumes the invocation that made the call; all responses in one message must belong to one invocation; mixed text+function-response messages raise `ValueError` on `LlmAgent`/`Workflow` roots; resumed tool calls run "at least once". 2.12 unified `decide_step_resume` and routes user FunctionResponses to sub-agents when `is_resumable=False`. The resume-of-an-ENDED-invocation behavior that forced our monkeypatch is **not documented** — must be re-measured (the 1.31 repro exists: `test_confirmation_resume_resumable.py`).

### 2.4 Plugins and callbacks
- 14 hooks (was 12): `+on_agent_error_callback`, `+on_run_error_callback`. No removals.
- Semantics tightened: `after_run_callback` now runs on aborted/cancelled runs and early generator exit (2.10/2.12); `after_agent_callback` on cancellation; `before_tool_callback` moved into per-tool execution and its contextvars propagate to the tool (2.9); `after_tool_callback` can replace tool output (2.12); `on_tool_error`/`on_model_error` fire on cancellation (2.13/2.14).

### 2.5 Tools and confirmation
- `ToolContext = Context` (alias); `request_confirmation(hint, payload)` unchanged; `REQUEST_CONFIRMATION_FUNCTION_CALL_NAME` moved to `utils/_function_call_names.py` but is re-exported from `flows/llm_flows/functions.py` (our import path still works).
- `BaseTool.check_require_confirmation` + callable `require_confirmation` (receives raw args); workflow tool nodes pause via `RequestInput` (2.11); HITL pauses surface in `AgentTool` (2.13).
- **Breaking (2.14)**: `FunctionTool` Enum-annotated args arrive as enum members. adk-cc's tool schemas use no `Enum` types (grep clean).
- MCP: `McpSessionManager` gained HTTP client wrapper, session-terminated detection, header redaction (additive).

### 2.6 Skills (the largest overlap with adk-cc)
- `SkillToolset(skills, *, registry, code_executor, environment: BaseEnvironment, skills_folder, script_timeout, save_output_artifacts, additional_tools, tool_name_prefix, tool_filter, discovery_mode: LAZY|EAGER, lifecycle_config: PERSISTENT|BOUNDED|EPHEMERAL)`; new `SearchSkillsTool`, `UnloadSkillTool`; `_ensure_skill_materialized_in_env`, `_write_skill_resources_to_env`, `_materialize_and_run`, `_build_script_cli_args`, `_script_interpreter_for_path`; skill-script outputs persisted as artifacts (2.12); content-hash based materialization; `skills_folder` property.
- **Breaking (2.14)**: `RunSkillScriptTool` in environment mode takes `args` | `short_options` + `positional_args` (no free-form `command`), runs **from the skill directory**, supports only `.py/.sh/.bash` (others → `UNSUPPORTED_SCRIPT_TYPE`), old calls → `INVALID_ARGUMENTS`. Rationale: prompt-injection containment.
- `google.adk.skills` now exports `load_skill_from_dir`, `list_skills_in_dir`, `load_skills_from_dir` (+ `_async`, `+gcs` variants); `Skill` model gained `_uri`.
- **adk-cc impact**: our launcher (`_wrapper` + materialization under `.adk-cc/skill-runtime/<skill>/<digest>/`, lazy deps, `SKILL_DATA_DIR`, trust gate, #114 script gate, #131 process-registry streaming, `.mjs/.ts/.ps1/.rb` support) duplicates what upstream now does in `environment` mode — but with more languages, workspace-cwd semantics (scripts see the workspace, not the skill dir), and our security gates. This is a design decision, not a mechanical port (§5 P3).

### 2.7 Models
- `BaseLlm.capabilities` (`LlmCapabilities`), `connect()` signature changed; `_legacy_output_schema_and_tools`.
- LiteLlm: JSON-tolerant tool-call argument parsing (2.12), malformed args reported instead of raised (2.10), "stop running tool calls quoted in answer text", stream-without-finish-reason reported as truncated (2.14); `thinking_config` forwarded; litellm capped `<1.106.3` (2.13) — adk-cc declares `litellm>=1.50` with no upper bound (1.87.0 installed, inside the cap); pin inside the cap when adopting `google-adk[extensions]`. `_message_to_generate_content_response(message, *, is_partial, model_version, thought_parts)` — same signature we wrap.
- `labs/`: `OpenAIDecisionsClassifier` (classifies workflow/tool-gating questions via the OpenAI Decisions API — relevant to the parked auto-mode track), `antigravity`, `classifiers`.

### 2.8 Server, telemetry, flags
- `get_fast_api_app` kept; new kwargs `bind_host`, `task_store_uri`, `trigger_oidc_*`, `trigger_auth_verifier`, `default_llm_model`, `gemini_enterprise_app_name`, `express_mode`, `avatar_config`, `max_llm_calls`. `AdkWebServer` deprecated; `api_server` monitors `http.disconnect` and aborts the run.
- Telemetry defaults to OTel GenAI semconv v2 (2.13); `call_llm` span removed (`ADK_TELEMETRY_SCHEMA_VERSION_OPT_IN=otel_semconv_1_36` restores).
- 16 toolset/integration features promoted to stable: `ADK_DISABLE_*` / `--disable_features` silently ignored for them. adk-cc sets none.
- 1.x line is frozen: last release 1.39.1 (2026-08-27). Staying on 1.31.1 means no fixes at all.

---

## 3. Compatibility verdict per adk-cc subsystem

| Subsystem | Verdict | Why |
|---|---|---|
| Plugins (32) | **compatible, re-verify semantics** | all hooks exist; `after_run` now fires on abort/early exit (affects MemoryPlugin capture, SessionTitle persist, NotesPlugin); `before_tool` per-tool ordering (PermissionPlugin's confirmation waves #114/#122). |
| Tools (43) | **compatible** | `BaseTool`/`BaseToolset` additive; no Enum args; `AdkCcTool.run_async` pattern unchanged. |
| Sandbox backends, process registry | **compatible** | pure adk-cc; only `BaseCodeExecutor` contract (unchanged). |
| Turn Broker | **adapt** | adopt `abort_signal` + upstream sealing; `_resolve_invocation_id` patch must be re-measured; `extract_adk_web_server` must go. |
| FileSessionService | **compatible, verify** | new Event fields round-trip through `model_dump/model_validate`; add `flush()`; consider `StaleSessionError`. |
| Compaction defense (#101/#102/#119/#128) | **re-measure** | runner-owned compaction, earlier token firing, per-session config hook, call/response range rule. |
| Skills subsystem | **rebuild (design decision)** | `RunSkillScriptTool` contract broke; upstream environment-mode materialization overlaps ours. |
| Confirmation UI / resume (#114, #115, #119) | **re-measure** | upstream reworked resume resolution (2.12); our monkeypatch may be obsolete or harmful. |
| LiteLlm shims (`tolerant_tool_json`, `session_retry`) | **likely remove** | upstream tolerant parsing + malformed reporting; verify with the suites that motivated them. |
| Server (`server.py`) | **adapt** | `get_fast_api_app` kept; share services without closure-scanning (`service_registry`, or compose `api_server`). |
| Web/desktop clients | **compatible** | `RunEvent` tolerates unknown fields; UI should ignore/optionally render `node_info`. |

---

## 4. Rules adk-cc currently breaks under 2.x semantics

1. **Direct `append_event` from plugins/broker** (`midturn_messages`, `session_title`, `turns.py` orphan prune + interrupted-call seal). These persist but bypass `on_event_callback`, streaming, and the node graph's bookkeeping. Under 2.x, the sanctioned shapes are: yield events from the agent/node, or use the runner's own paths (`yield_user_message`, sealing). Decision: keep `append_event` only where the runner offers no hook (mid-turn user events — the message *is* a user turn and must persist before the next model call), and move the rest onto runner features (abort sealing replaces the hand-written seal; orphan prune becomes a rewind/`delete_last_event` call). Corollary for the broker: 2.x seals an aborted invocation *into history*, so application-side bookkeeping that exists only to reconstruct what the framework now records (`Turn._pending_calls`/`_pending_authors` as used by the seal, orphan detection) becomes redundant — keep broker-side tracking only for live UI status (`current_tool`, progress) and read everything about finished or aborted invocations from events.
2. **Monkeypatching runner/toolset/lite_llm internals.** Each must either be removed (upstream covers it) or moved behind `adk_cc/compat/adk.py` with a pin test per symbol.
3. **Catching broad exceptions inside tools** disables upstream retries (2.0 page). adk-cc tools return structured errors by design (the model must see them); keep that, but do not catch `BaseException` anywhere (grep: none).

---

## 5. Migration plan

Branch `migrate/adk-2.14` (substantial, separate branch per house rule). Each phase has an exit criterion; nothing merges to `main` before P2.

**P0 — Spike (1–2 days).** Pin `google-adk==2.14.0`; run the 23 runner-based suites + the plugin/tool unit suites; record every failure with its cause class (import, signature, behavior). Create `agents/adk_cc/compat/adk.py` and route **every** private touchpoint in §1 through it, each with a pin test that fails loudly on rename. Exit: a failure inventory with owners; compat module in place.

**P1 — Compatibility, no behavior change.** Imports/re-exports; `FileSessionService.flush`; Event round-trip test with `node_info`/`output`/`isolation_scope`; `get_fast_api_app` call updated; remove `extract_adk_web_server` by obtaining services from `get_service_registry()`/explicit construction. A/B each workaround against its original repro: confirmation-resume fix (`test_confirmation_resume_resumable` on 2.14 with the patch OFF), `tolerant_tool_json` (its malformed-JSON suite with the shim OFF), `session_retry`, #119 compaction-eats-confirmation pin. Keep a workaround only if its repro still fails. Exit: unit suites green on 2.14; live battery (`tests/live_battery.py`) green on desktop noop; no monkeypatches without a pin test.

**P2 — Runtime adoption.** Turn Broker: pass `abort_signal` instead of cancelling the task; rely on upstream sealing (`_synthesize_abort_events_if_needed`) and delete `_close_interrupted_calls` once `test_midturn_messages::test_interrupt_mid_tool_closes_the_call` passes without it; decide the Stop-with-pending-confirmation semantics (upstream seals the confirmation request too — either accept "Stop cancels the pending confirmation" in the UI, or keep our narrower seal for that case only); re-check the paused-task-reply skip against send-now. Compaction: configure via a `before_run_callback` (per-session), recalibrate ContextGuard thresholds to the earlier token-threshold firing (#128 ladder), re-run the compaction e2es. Verify `after_run`-on-abort doesn't capture memory from aborted turns (extend `_confirmation_pending`-style guard). Exit: durable-runs, confirmation, mid-turn, compaction e2es green.

**P3 — Skills re-platform (design first).** Options: (a) keep adk-cc's launcher, adapt only to the `RunSkillScriptTool` declaration (`args`/`positional_args`), and keep running from the workspace cwd; (b) implement `BaseEnvironment` over adk-cc's sandbox backends and let upstream materialize/run, layering trust/#114/deps/`SKILL_DATA_DIR` as env + plugin hooks; (c) hybrid — upstream for `.py/.sh/.bash`, ours for the rest. Decide after measuring (a) vs (b) on `test_skill_script_languages`, `test_skill_deps`, `e2e_skill_script_adversarial`, the client's shrinkage/psycopg2 cases. Non-negotiables regardless of option: workspace-relative paths keep working for the model, lazy deps, the trust gate, streaming into the process registry. Exit: skill suites green; adversarial e2e 4/4; client scenario re-run.

**P4 — Server.** Replace `AdkWebServer` reliance with `api_server`/`dev_server` composition (or own routes on a `Runner`); adopt `http.disconnect` abort; `GetSessionConfig(num_recent_events)` for long sessions in the broker. Exit: web + desktop e2e UI suites green; remote deployment dry-run.

**P5 — Opportunities (after merge).** `OpenAIDecisionsClassifier`/`labs.classifiers` as the auto-mode classifier backend; `Workflow` nodes for the verify-gate loop (#66–#69) and dogfood batteries; `LlmAgent.mode="task"` evaluation for specialists; dynamic instruction routing (experimental, off by default).

**Verification matrix (every phase):** `test_turn_broker`, `test_turn_prune`, `test_resumability_f3`, `test_confirmation_resume_resumable`, `test_midturn_messages`, `test_memory_*`, `test_skill_*`, `test_process_registry`, `test_context_pressure`/compaction suites; live: `e2e_busy_409_ui`, `e2e_midturn_ui`, `e2e_confirmation_*`, `e2e_skill_script_adversarial`, `e2e_container_background`, `e2e_datasets_*`, `live_battery.py`. Rule: a removed workaround ships only with an A/B showing the original repro passes without it.

---

## 6. Risks and decisions

- **Session storage format** (one-way door for DB users; JSONL is fine). Decide before P1 whether web deployments stay on `FileSessionService`/sqlite or move to `DatabaseSessionService` (then run `sessions/migration/`).
- **Earlier compaction** can collide with the ContextGuard/precompact ladder — recalibrate, don't stack.
- **`after_run` on abort** changes what memory/title/notes plugins see; guard explicitly.
- **Skills**: option (b) is the largest rewrite in the codebase (≈2.6K lines) and the client's daily path — stage behind a flag, A/B live.
- **AdkWebServer deprecation** will eventually remove the closure the broker scans; P1 must not depend on it.
- **Stale 1.x**: security/bug fixes only land on 2.x — do the spike now even if P3 waits.

## 7. Open items to verify on the branch (not assumptions)
Event round-trip incl. `isolation_scope` · resume of an ended gated invocation on 2.14 · Stop-with-pending-confirmation UX under upstream sealing · telemetry semconv change vs our audit/usage plugins · `session_retry` under 2.14 (its "stale session"/"modified in storage" markers still match the 2.14 sqlite/database messages, but catching `StaleSessionError` by type is the right fix).

Verified while writing (no longer open): `apps.app`/`apps` re-export both configs · #119 range rule is handled upstream · `session_retry` target is `append_event` on the sqlite/database services · `agent.py` has no code dependence on `_find_agent_to_run` · litellm: adk-cc has no upper bound, 2.14 extras cap `<1.106.3`.
