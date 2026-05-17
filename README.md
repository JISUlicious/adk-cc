# adk-cc — data-science agent variant

A small **explore → plan → act → verify** agent loop built on Google ADK 1.31.1, scoped to the data-science use case: a coordinator dispatches to five specialist sub-agents (loader, explorer, processor, visualizer, critic) and gates its final answer through an independent critic.

This is the `feat/data-science-agent` branch — a focused fork of `main`. It has zero filesystem / shell / web tools; the agent runs in environments where those surfaces aren't available. Tools are in-memory data operations.

Deeper detail in [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md).

## What it is

- **Coordinator** (the only agent that talks to the user) owns four loop-control tools: `record_plan`, `read_plan`, `verify_completion`. No analysis tools of its own — all data work happens in specialists.
- **Five sub-agents** wired via `transfer_to_agent`, each `disallow_transfer_to_parent=True` + `disallow_transfer_to_peers=True`, hand control back via an `after_agent_callback` that yields a synthetic function-call so the parent flow loop survives the specialist's final text:
    - `loader` — `load_from_registry`, `load_from_db_mock`, `load_from_file_mock`
    - `explorer` — `list_datasets`, `describe_dataset`, `peek_dataset`, `profile_dataset`
    - `processor` — `filter_dataset`, `aggregate_dataset`, `correlate`, `drop_na`, `transform_column`, `select_columns`
    - `visualizer` — `render_bar_chart`, `render_table`, `summarize_distribution`
    - `critic` — independent verifier; no tools; emits a structured `CriticVerdict` (verdict / addressed_aspects / missing_aspects / evidence_quality / reasoning) via ADK's `output_schema` enforcement
- **Loop discipline** lives in `StageGuardPlugin`: soft nudges via `before_model_callback` (a `<stage-nudge>` block prepended to system_instruction telling the model which stage it's in), stage transitions emitted as audit events on `after_tool_callback`. No hard gates — the durable PASS/FAIL gate is `verify_completion`'s rule check + critic verdict combined.
- **Plugin chain (5)**: AuditPlugin → StageGuardPlugin → ToolCallValidatorPlugin → ContextGuardPlugin → ModelIOTracePlugin. AuditPlugin always observes; ToolCallValidatorPlugin catches "tool not found" errors and synthesizes a corrective response so the model self-corrects; ContextGuardPlugin pre-flights token budget; ModelIOTracePlugin is opt-in (`ADK_CC_LOG_MODEL_IO=1`).
- **Independent verification**: `verify_completion` combines a deterministic rule-check (plan recorded, result count ≥ plan length, conclusion non-empty) with the critic's structured judgment. PASS requires BOTH to agree. The critic optionally runs on a different model via `ADK_CC_CRITIC_*` env vars.

## Layout

```
adk-cc/                              ← repo root
├── pyproject.toml
├── .env.example                     ← env-var reference
├── README.md
├── docs/ARCHITECTURE.md             ← deeper architecture write-up
├── examples/
│   ├── data_science_agent.py        ← happy-path scripted demo
│   └── data_science_agent_recovery.py ← critic-FAIL → recovery scripted demo
├── tests/                           ← 6 surviving suites (see "Tests" below)
└── adk_cc/                          ← agent package (3.7k LoC total)
    ├── __init__.py                  ← `from . import agent`
    ├── agent.py                     ← coordinator + plugin chain + App
    ├── prompts.py                   ← coordinator instruction
    ├── logging_setup.py             ← env-driven logging config
    ├── permissions/                 ← just `token_counter.py` (used by ContextGuardPlugin)
    ├── plugins/                     ← 6 plugins (audit, stage_guard, tool_call_validator,
    │                                  context_guard, model_io_trace, session_retry)
    ├── sub_agents/                  ← one subpackage per specialist
    │   ├── _shared.py               ← `make_specialist_model`, `make_critic_model`,
    │   │                              `force_coordinator_continuation`
    │   ├── loader/  · agent.py · prompts.py · tools/{load_from_registry, db_mock, file_mock}.py
    │   ├── explorer/                · tools/{list_datasets, describe, peek, profile}.py
    │   ├── processor/               · tools/{filter, aggregate, correlate, drop_na, transform, select}.py
    │   ├── visualizer/              · tools/{render_bar_chart, render_table, summarize_distribution}.py
    │   └── critic/                  · agent.py · prompts.py · schema.py (CriticVerdict)
    └── tools/                       ← coordinator tools + shared infrastructure
        ├── base.py                  ← AdkCcTool + ToolMeta
        ├── datasets.py              ← in-memory dataset registry
        ├── loop_state.py            ← `record_load`, `stash_result` helpers
        ├── planning.py              ← RecordPlanTool, ReadPlanTool
        └── verification.py          ← VerifyCompletionTool (rule-check + critic combiner)
```

## Quick start

```bash
cd adk-cc

# Install dependencies (uv recommended; `uv pip install -e .` also works).
uv venv .venv && source .venv/bin/activate
uv pip install -e .

# Copy env reference and fill in your model server config.
cp .env.example .env
$EDITOR .env  # set at minimum ADK_CC_API_KEY

# Run the api_server. ADK CLI discovers `adk_cc.agent.root_agent` automatically.
uv run adk api_server . --host 127.0.0.1 --port 8765
```

In a second shell:

```bash
SESSION=$(curl -s -X POST http://127.0.0.1:8765/apps/adk_cc/users/alice/sessions \
  -H 'Content-Type: application/json' -d '{}' | jq -r '.id')

curl -s -X POST http://127.0.0.1:8765/run -H 'Content-Type: application/json' -d '{
  "appName": "adk_cc",
  "userId": "alice",
  "sessionId": "'"$SESSION"'",
  "newMessage": {"role":"user","parts":[{"text":"List the datasets available, then tell me which sales_q1 region had the highest total revenue."}]}
}' | jq '.[-1].content.parts[].text // empty'
```

The agent walks `explore → plan → act → verify → done` and returns a markdown reply naming `south` as the highest-revenue region.

### Without a server

For a scripted-LLM walkthrough with no external model, the two demo files in `examples/` boot an in-process `InMemoryRunner` with a `BaseLlm` subclass that replays canned responses:

```bash
.venv/bin/python examples/data_science_agent.py            # happy path
.venv/bin/python examples/data_science_agent_recovery.py   # critic-FAIL → recovery
```

Each demo prints the transfer sequence, audit event counts, stage transitions, and the final coordinator reply. Useful for understanding the loop shape without spending model tokens.

## Configuration

11 env vars. Full list with defaults and rationale in [`.env.example`](./.env.example). Common ones:

| Var | Required? | Default | Meaning |
|---|---|---|---|
| `ADK_CC_API_KEY` | yes | — | Main model API key (read at module-load) |
| `ADK_CC_MODEL` | no | `openai/Qwen3.6-35B-A3B-UD-MLX-4bit` | LiteLLM model id |
| `ADK_CC_API_BASE` | no | `http://localhost:18000/v1` | OpenAI-compatible base URL |
| `ADK_CC_CRITIC_MODEL` | no | falls back to `ADK_CC_MODEL` | Independent model for the critic sub-agent |
| `ADK_CC_AUDIT_LOG` | no | `~/.adk-cc/audit.jsonl` | JSONL audit sink path |
| `ADK_CC_LOG_MODEL_IO` | no | unset (off) | Set `1` to dump LlmRequest/LlmResponse to audit |
| `ADK_CC_MAX_CONTEXT_TOKENS` | no | unset (off) | Enable ContextGuardPlugin's pre-flight budget |
| `ADK_CC_SESSION_RETRY_ON_STALE` | no | unset (off) | Set `1` to monkey-patch ADK session services with retry-on-stale |
| `ADK_CC_LOG_LEVEL` | no | `INFO` | Python log level for `adk_cc.*` |
| `ADK_CC_LOG_FORMAT` | no | `text` | `text` or `json` |

## Tests

Six suites, all green. Run individually or all at once:

```bash
.venv/bin/python tests/test_data_science_agent.py             # happy-path e2e (scripted)
.venv/bin/python tests/test_data_science_agent_recovery.py    # critic-FAIL → recovery e2e (scripted)
.venv/bin/python tests/test_logging_setup.py
.venv/bin/python tests/test_model_io_trace.py
.venv/bin/python tests/test_session_retry.py
.venv/bin/python tests/test_token_counter.py
```

The two e2e suites subprocess-boot the corresponding `examples/` demo and assert the transfer sequence, stage transitions, audit event counts, and final reply against a fixed reference.

## Audit trail

Every tool call lands in `~/.adk-cc/audit.jsonl` (override via `ADK_CC_AUDIT_LOG`) as one JSON object per line. Event types this branch emits:

- `tool_call_attempt` — every dispatched tool call (incl. transfers)
- `tool_call_result` — after the tool returns
- `tool_call_error` — execution raised (ToolCallValidatorPlugin recovers from "tool not found")
- `loop_stage_transition` — `null→explore`, `explore→plan`, `plan→act`, `act→verify`, `verify→done`
- `model_request` / `model_response` — when `ADK_CC_LOG_MODEL_IO=1`, full LlmRequest / LlmResponse dumps

Tail it during a run to see the agent's behavior in real time:

```bash
tail -F .audit/audit.jsonl | jq -c '{event, tool_name, from, to}'
```

## Diverged from `main`

This branch is a deliberate fork of `main` with ~5.6k LoC removed (60% smaller). Compared to `main`:

- No filesystem tools (`read_file`, `glob_files`, `grep`, `write_file`, `edit_file`)
- No shell or web tools (`run_bash`, `web_fetch`)
- No task tracking, no skills, no MCP, no project context auto-load
- No HITL confirmation flow, no permission engine, no sandbox backends
- No multi-tenant tenancy, no JWT/Bearer auth, no quota plugin
- No production FastAPI factory beyond what `adk api_server` provides natively

What's gained: a small focused tree where every file has a live importer and the live-run path (~140s on minimax-m2.7) is verifiable end-to-end against a real LLM.

If you want the production-grade scaffolding (sandboxing, multi-tenancy, permission gating), see `main` branch.
