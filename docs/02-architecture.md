# Architecture

## 1. File layout

```
adk-cc/                          ← AGENTS_DIR (the path you point `adk web` at)
├── pyproject.toml               # google-adk==1.31.1, litellm>=1.50
├── README.md
├── docs/                        # this directory
└── adk_cc/                      # the agent module ADK discovers
    ├── __init__.py              # `from . import agent`
    ├── agent.py                 # exposes `app` (preferred) and `root_agent`
    ├── prompts.py               # per-agent system prompts
    ├── tools/                   # AdkCcTool subclasses (Stage A) + integrations (Stage E)
    │   ├── base.py              # AdkCcTool, ToolMeta
    │   ├── schemas.py           # Pydantic input models
    │   ├── _fs.py               # workspace-aware path resolver (Stage C)
    │   ├── read_file.py
    │   ├── glob_files.py
    │   ├── grep.py
    │   ├── write_file.py
    │   ├── edit_file.py
    │   ├── bash/{tool,prompt}.py
    │   ├── web_fetch.py         # preapproved-hosts URL fetcher (Stage E)
    │   ├── ask_user_question.py # long-running multi-choice HITL (Stage E)
    │   ├── mcp.py               # make_mcp_toolset() factory (Stage E)
    │   ├── skills.py            # SkillToolset auto-loader (Stage E)
    │   └── task/                # 5 task tools (Stage F)
    │       ├── create.py
    │       ├── get.py
    │       ├── list.py
    │       ├── update.py
    │       └── stop.py
    ├── skills/                  # optional: skill folders here are auto-loaded
    ├── permissions/             # rule engine (Stage B)
    │   ├── modes.py             # PermissionMode enum
    │   ├── rules.py             # PermissionRule + per-tool fnmatch matchers
    │   ├── settings.py          # SettingsHierarchy (policy/user/project/session)
    │   └── engine.py            # decide() — 4-step flow
    ├── sandbox/                 # isolation backend (Stage C)
    │   ├── config.py            # FsRead/Write/NetworkConfig + ExecResult
    │   ├── workspace.py         # WorkspaceRoot — per-(tenant,session) FS root
    │   └── backends/
    │       ├── base.py          # SandboxBackend ABC
    │       ├── noop_backend.py  # host execution (dev only); async via asyncio.subprocess
    │       ├── docker_backend.py # stub for self-host
    │       └── e2b_backend.py   # stub for hosted production
    ├── tasks/                   # background task system (Stage F)
    │   ├── model.py             # Task, TaskStatus, blocks/blocked_by edges
    │   ├── storage.py           # TaskStorage ABC + InMemoryTaskStorage
    │   └── runner.py            # TaskRunner — asyncio.Task pool worker
    ├── plugins/                 # ADK BasePlugin integrations
    │   ├── permissions.py       # PermissionPlugin (Stage B)
    │   ├── audit.py             # AuditPlugin (Stage D) — JSONL or callable sink
    │   └── quotas.py            # QuotaPlugin (Stage G) — per-tenant rate cap
    ├── service/                 # web-service deployment (Stage G)
    │   ├── tenancy.py           # TenantContext + TenancyPlugin (state seeder)
    │   ├── auth.py              # AuthExtractor protocol + BearerTokenExtractor
    │   └── server.py            # build_fastapi_app() + make_app() factory
    └── config/
        └── settings_loader.py   # YAML → SettingsHierarchy (Stage G)
```

`adk web` / `adk run` look for `app` first, then `root_agent`. Stage B adds `app = App(name=..., root_agent=root_agent, plugins=[PermissionPlugin(...)])` so the plugin chain is wired automatically; direct imports of `root_agent` (e.g. for tests) keep working unchanged.

ADK's `adk web` / `adk run` looks for an immediate child directory of the AGENTS_DIR with `__init__.py` and `agent.py`. The module-level name `root_agent` in `agent.py` is the entry agent.

## 2. Agent topology

```
coordinator (LlmAgent, root)
│   tools: read_file, glob_files, grep, write_file, edit_file, run_bash
└── sub_agents:
    ├── Explore         (LlmAgent, read-only)  tools: read_file, glob_files, grep
    ├── Plan            (LlmAgent, read-only)  tools: read_file, glob_files, grep
    └── verification    (LlmAgent, /tmp-only)  tools: read_file, glob_files, grep, run_bash
```

Hub-and-spoke. The coordinator is the only agent that talks to the user; specialists are leaves. Each specialist has:

- `disallow_transfer_to_parent=True`
- `disallow_transfer_to_peers=True`
- `after_agent_callback=_force_coordinator_continuation`

Delegation is via ADK's auto-injected `transfer_to_agent` tool. The coordinator's prompt names each specialist by `agent.name` — that's the routing table. There are no `AgentTool` wrappers; specialists are wired into `sub_agents=[...]`, which makes them share the parent's invocation context (and so their events stream into `adk web`'s UI).

## 3. Coordinator-owns-user-I/O (dual mechanism)

ADK's defaults do not enforce that sub-agents never address the user. Two distinct mechanisms cover the two failure modes; **neither alone is sufficient**.

### 3.1 Cross-turn: `disallow_transfer_to_parent=True`

When the user sends the next message, ADK's runner needs to decide which agent's turn it is. The relevant code:

```python
# google/adk/runners.py — Runner._find_agent_to_run
for event in filter(_event_filter, reversed(session.events)):
    if event.author == root_agent.name:
        return root_agent
    if not (agent := root_agent.find_sub_agent(event.author)):
        continue
    if self._is_transferable_across_agent_tree(agent):
        return agent
return root_agent
```

`_is_transferable_across_agent_tree` returns `False` for any agent whose `disallow_transfer_to_parent` is `True`. So a specialist is **skipped** as a candidate, and the runner walks back further or falls through to the root. Net effect: the next user message is always routed to the coordinator.

Side effect: ADK's auto-injected transfer instruction (`google/adk/flows/llm_flows/agent_transfer.py:_get_transfer_targets`) only lists the parent as a transfer target when `disallow_transfer_to_parent=False`. With it set to `True`, the specialist's prompt also doesn't mention the parent at all — there's no temptation to "transfer back."

### 3.2 Same-turn: `after_agent_callback`

Within a single turn, when a specialist finishes, ADK's flow loop checks whether to continue:

```python
# google/adk/flows/llm_flows/base_llm_flow.py — BaseLlmFlow.run_async
while True:
    last_event = None
    async for event in self._run_one_step_async(...):
        last_event = event
        yield event
    if not last_event or last_event.is_final_response() or last_event.partial:
        break
```

If the specialist's last event is a text-only message, `is_final_response()` returns `True` → the loop breaks → the user sees the specialist's text directly. We don't want that.

`Event.is_final_response()` (in `google/adk/events/event.py`) returns `False` when the event has function calls. So the after-agent callback returns a `Content` whose only `Part` is a synthetic `function_call`:

```python
# adk_cc/agent.py — _force_coordinator_continuation
def _force_coordinator_continuation(callback_context):
    return types.Content(
        role="model",
        parts=[types.Part(function_call=types.FunctionCall(
            name="_handback_to_coordinator",
            args={},
        ))],
    )
```

This event is not final → flow loops → coordinator's LLM is invoked again with the conversation history (including the specialist's report) → coordinator produces the user-facing reply.

The synthetic call name (`_handback_to_coordinator`) has no handler. It's a control signal, not a tool invocation. Most LLMs handle dangling function calls gracefully and just respond with text on the next step.

### 3.3 Why both are needed

- Without §3.1 (cross-turn), the next user message could land on a specialist if the specialist was the last non-user event author and was transferable.
- Without §3.2 (same-turn), the specialist's text-only final message would be shown to the user as the visible reply for the current turn, with the coordinator never getting a chance to synthesize.

## 4. Tool denylist via tool surface

There is no plugin or hook that says "the verifier cannot edit files." Each agent's `LlmAgent.tools=[...]` simply lists the functions it has access to:

| Agent | `tools=[...]` |
|---|---|
| coordinator | `read_file, glob_files, grep, write_file, edit_file, run_bash` |
| Explore | `read_file, glob_files, grep` |
| Plan | `read_file, glob_files, grep` |
| verification | `read_file, glob_files, grep, run_bash` |

`AgentTool` is **not** in any `tools` list. Combined with `disallow_transfer_to_peers=True`, this means specialists cannot delegate or recurse.

The verifier has `run_bash` (it needs to run builds/tests), but its prompt says project-directory writes are prohibited and `/tmp` is allowed. This is prompt-enforced, not structural — at the runtime level, a misbehaving verifier could write anywhere.

## 5. Verification gate contract

The verifier's prompt requires its final report to end with one of:

```
VERDICT: PASS
VERDICT: FAIL
VERDICT: PARTIAL
```

The coordinator's prompt instructs it to:

- Read this line from the conversation history.
- On `FAIL`: fix, re-transfer to verification with the original request + the fix. Repeat until `PASS`.
- On `PASS`: spot-check by re-running 2–3 commands from the verifier's report.
- On `PARTIAL`: report what passed and what couldn't be verified.

There is **no code-level parser** for the verdict. The contract is a prompt rule pair: the verifier produces the line, the coordinator acts on it. The architectural choice mirrors Claude Code's upstream pattern (see [03-prompts.md](./03-prompts.md) for the lineage).

## 6. Local model wiring

ADK's `LiteLlm` wrapper (`google.adk.models.lite_llm.LiteLlm`) forwards kwargs to LiteLLM's completion API. The `MODEL` is constructed once and shared across all four agents:

```python
MODEL = LiteLlm(
    model=os.environ.get("ADK_CC_MODEL", "openai/Qwen3.6-35B-A3B-UD-MLX-4bit"),
    api_base=os.environ.get("ADK_CC_API_BASE", "http://localhost:18000/v1"),
    api_key=os.environ["ADK_CC_API_KEY"],
)
```

The model id uses the `openai/` prefix because the target server is OpenAI-compatible. Any LiteLLM-supported backend will work via env-var override (`ollama_chat/...`, `anthropic/...`, etc.) — no code changes needed.

## 7. What ADK does for us

We rely on ADK 1.31.1 for:

- Agent discovery and the `adk web` / `adk run` runners.
- The `transfer_to_agent` tool (auto-injected for any agent with `sub_agents`).
- The flow loop, function-call handling, event streaming, session storage.
- `LiteLlm` for non-Gemini backends.

We do **not** use:

- `AgentTool` — would hide specialist events from the parent's stream.
- ADK plugins — none configured.
- Workflow agents (`SequentialAgent`, `LoopAgent`, `ParallelAgent`) — we want model-driven routing, not hard-coded sequencing.
- `output_schema` / `output_key` — specialists return free-form text reports.
