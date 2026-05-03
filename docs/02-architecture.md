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
    │   ├── enter_plan_mode.py   # flips permission_mode to "plan"
    │   ├── exit_plan_mode.py    # gated on user approval; flips back to default
    │   ├── write_plan.py        # persist plan as Markdown artifact
    │   ├── read_current_plan.py # read latest plan from session state
    │   ├── mcp.py               # make_mcp_toolset() factory (Stage E)
    │   ├── skills.py            # SkillToolset auto-loader (Stage E)
    │   └── task/                # 4 task tools (Stage F — tracking only)
    │       ├── create.py
    │       ├── get.py
    │       ├── list.py
    │       └── update.py
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
    │       ├── docker_backend.py # remote-Docker per-session containers (Stage C)
    │       └── e2b_backend.py   # stub for hosted Firecracker microVMs (future)
    ├── tasks/                   # task tracking (Stage F — pure tracking, no execution)
    │   ├── model.py             # Task, TaskStatus (3 statuses), blocks/blocked_by
    │   ├── storage.py           # TaskStorage ABC + InMemoryTaskStorage + JsonFileTaskStorage
    │   └── runner.py            # TaskRunner — thin storage facade (default: JsonFileTaskStorage)
    ├── plugins/                 # ADK BasePlugin integrations
    │   ├── permissions.py       # PermissionPlugin (Stage B)
    │   ├── audit.py             # AuditPlugin (Stage D) — JSONL or callable sink
    │   ├── plan_mode.py         # PlanModeReminderPlugin — dynamic tool filter + planning instruction
    │   ├── task_reminder.py     # TaskReminderPlugin — periodic task-list system reminder
    │   ├── quotas.py            # QuotaPlugin (Stage G) — per-tenant rate cap
    │   └── tool_call_validator.py # converts "tool not found" into corrective tool responses
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
│   tools: read/write/exec + task tools + plan-mode tools
│          + ask_user_question + web_fetch + read_current_plan + write_plan
│          + (auto-loaded) skills, MCP toolsets
└── sub_agents:
    ├── Explore         (LlmAgent, read-only)  tools: read_file, glob_files, grep, web_fetch
    └── verification    (LlmAgent, /tmp-only)  tools: read_file, glob_files, grep, run_bash, web_fetch, read_current_plan
```

Hub-and-spoke. The coordinator is the only agent that talks to the user; specialists are leaves. Planning is **not** a specialist — when the coordinator calls `enter_plan_mode`, the `PlanModeReminderPlugin` dynamically filters write/exec/task tools out of the LLM's tool surface and injects a planning instruction. The coordinator becomes a planning agent in-place; no transfer ceremony. See [§3.5](#35-plan-mode-as-coordinator-posture).

Each specialist has:

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

## 3.5 Plan mode as coordinator posture

Plan mode is a session-wide flag (`state["permission_mode"] == "plan"`) flipped by `enter_plan_mode` and back by `exit_plan_mode` (the latter gated on user approval via `ToolMeta.requires_user_approval=True`).

While the flag is set, `PlanModeReminderPlugin.before_model_callback` does two things on every coordinator LLM call:

1. **Tool surface filtering.** Removes `write_file`, `edit_file`, `run_bash`, `task_create`, `task_update`, `enter_plan_mode` from BOTH `llm_request.tools_dict` AND each `tool_obj.function_declarations`. (Both surfaces feed the model, so filtering only one leaks the tool.) `exit_plan_mode` is filtered out when NOT in plan mode (nothing to exit).
2. **Instruction injection.** Appends a planning `<system-reminder>` to `llm_request.config.system_instruction`: 4-step process (understand / explore / design / detail), required `write_plan` output format, and the `exit_plan_mode` approval contract.

The coordinator's tool list itself is unchanged — `agent.py` registers the same 16 tools regardless of mode. The plugin is the only mechanism that narrows what the LLM sees per-turn. This means:

- No agent rewiring per session, no separate "planning agent" instance.
- The model can't call what it can't see.
- If the model hallucinates a hidden tool name anyway, `ToolCallValidatorPlugin` catches the resulting "tool not found" error (see §7) and returns a corrective tool response — the loop continues, the model self-corrects.

History note: an earlier design routed planning through a `Plan` sub-agent invoked via `transfer_to_agent`. That mechanism overlapped with `enter_plan_mode` (both produced "plan, then act with user approval"); the redundancy caused the model to do both. The unification collapses planning into a single posture.

## 4. Tool denylist via tool surface

There is no plugin or hook that says "the verifier cannot edit files." Each agent's `LlmAgent.tools=[...]` simply lists the functions it has access to:

| Agent | `tools=[...]` |
|---|---|
| coordinator | full surface — read tools, `write_file`, `edit_file`, `run_bash`, task tools, plan-mode tools (`enter_plan_mode`, `exit_plan_mode`, `write_plan`, `read_current_plan`), `ask_user_question`, `web_fetch`, plus auto-loaded skills/MCP. `PlanModeReminderPlugin` narrows this dynamically when `permission_mode == "plan"`. |
| Explore | `read_file, glob_files, grep, web_fetch` |
| verification | `read_file, glob_files, grep, run_bash, web_fetch, read_current_plan` |

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

## 5.5. Sandbox layer (Stage C — DockerBackend lands here)

The sandbox layer (`adk_cc/sandbox/`) is the security boundary.
`SandboxBackend` is an abstract contract with five operations:
`exec`, `read_text`, `write_text`, `ensure_workspace`, `close`.
All host-touching tools (`run_bash`, `read_file`, `write_file`,
`edit_file`, `glob_files`, `grep`, `write_plan`, `read_current_plan`)
route through this contract — they never touch host FS / subprocess
directly.

**Implementations:**

- **`NoopBackend`** — host execution; honors path / network configs
  via Python checks. Dev-only; not a security boundary. Two safety
  guards: refuses to exec in production-shaped paths (anything outside
  `$HOME`, `/tmp`, OS tempdirs) unless `ADK_CC_NOOP_ACK_HOST_EXEC=1`
  is set; rejects non-existent or non-directory `cwd`. Same explicit-
  ack pattern as `make_app`'s `ADK_CC_ALLOW_NO_AUTH`.
- **`DockerBackend`** — connects to a (typically remote) Docker
  daemon and runs each session in its own container. Real isolation
  via Linux namespaces + cgroups + read-only rootfs + bind-mounted
  workspace + dropped capabilities. **Workspaces live on the sandbox
  host's filesystem**, not the agent pod's; the agent never opens
  workspace files via Python `Path`.
- **`E2BBackend`** — stub. Hosted Firecracker microVMs. Future.
- (future) `KubernetesBackend`, `ModalBackend`, `NsjailBackend`
  remain pluggable through the same ABC.

**Topology for production deployments:**

```
┌───────────────────────────┐         ┌─────────────────────────────┐
│  K8s cluster              │         │  Sandbox host (Linux VM)    │
│                           │         │                             │
│  ┌────────────────────┐   │ Docker  │  • Docker daemon            │
│  │ adk-cc agent pod   │───┼─TCP API─┤    (port 2375 plain or      │
│  │  - DockerBackend   │   │         │     2376 mTLS)              │
│  │    (remote client) │   │         │  • adk-cc-sandbox image     │
│  └────────────────────┘   │         │  • per-session containers   │
│                           │         │  • workspaces on local NVMe │
└───────────────────────────┘         │    /var/lib/adk-cc/wks/...  │
                                      └─────────────────────────────┘
```

**Connection modes** (picked by env vars at backend init):

| Mode | When | `ADK_CC_DOCKER_HOST` | TLS env vars |
|---|---|---|---|
| Unix socket | Agent + Docker on same host | `unix:///var/run/docker.sock` | unset |
| Plain TCP | Trusted internal network, single tenant | `tcp://host:2375` | unset |
| mTLS TCP | Untrusted segment, corporate policy | `tcp://host:2376` | all three set |

**Per-session container** (one per ADK session, lazy-spawn on first
tool call, torn down on session end):

```python
client.containers.run(
    image="adk-cc-sandbox:latest",        # configurable via ADK_CC_SANDBOX_IMAGE
    name=f"adk-cc-{session_id}",
    network_mode="none",                  # default deny
    mem_limit="4g",
    cpu_quota=100_000,                    # 1 CPU
    pids_limit=256,
    read_only=True,                       # rootfs immutable
    tmpfs={"/tmp": "size=1g,mode=1777"},
    volumes={ws.abs_path: {"bind": "/workspace", "mode": "rw"}},
    user="1000:1000",
    cap_drop=["ALL"],
    security_opt=["no-new-privileges"],
)
```

**Path translation.** Tools pass sandbox-host paths
(`<ws.abs_path>/foo`); the backend strips the workspace prefix and
prepends `/workspace`. The agent never sees the sandbox host's
filesystem otherwise — the backend is the only seam.

**Lifecycle.** `TenancyPlugin.before_tool_callback` calls
`backend.ensure_workspace(ws)` on first tool of a session; for
`DockerBackend` this runs a one-shot helper container to `mkdir -p`
the workspace dir on the sandbox VM. `TenancyPlugin.after_run_callback`
calls `backend.close()` on session end, which stops + removes the
per-session container.

### Hardware sizing (sandbox VM, single host)

For 100 users × tabular workloads (50K rows × ~1K cols, pandas /
numpy / sklearn): 16 physical cores, 96 GB RAM, 1 TB NVMe SSD.
~10 concurrent sessions × 4 GB sandbox = 40 GB peak. Linux host
running only the Docker daemon and adk-cc workload — no other tenants.

Scale past ~500 users by adding sandbox VMs and routing sessions via
consistent hashing on `session_id`.

## 6. Local model wiring

ADK's `LiteLlm` wrapper (`google.adk.models.lite_llm.LiteLlm`) forwards kwargs to LiteLLM's completion API. The `MODEL` is constructed once and shared across all three agents (coordinator, Explore, verification):

```python
MODEL = LiteLlm(
    model=os.environ.get("ADK_CC_MODEL", "openai/Qwen3.6-35B-A3B-UD-MLX-4bit"),
    api_base=os.environ.get("ADK_CC_API_BASE", "http://localhost:18000/v1"),
    api_key=os.environ["ADK_CC_API_KEY"],
)
```

The model id uses the `openai/` prefix because the target server is OpenAI-compatible. Any LiteLLM-supported backend will work via env-var override (`ollama_chat/...`, `anthropic/...`, etc.) — no code changes needed.

## 6.5. Tasks (Stage F — pure tracking)

Tracking-only after the refactor: no `command`/`output` fields, no
asyncio worker, no `task_stop` tool. The model uses `run_bash`
directly when it wants to run a command; tasks are just records of
"what work exists and where it stands." Three surfaces.

**Tools (`tools/task/`).** Four tools — `task_create`, `task_get`,
`task_list`, `task_update`. All non-destructive (state-mutating, but
not project-mutating), so they don't trigger the permission engine's
ask-on-destructive flow in DEFAULT mode. Schema mirrors upstream
Claude Code v2's `Task` (`src/utils/tasks.ts:76-89`): `id`, `title`,
`description`, `status` (one of `pending`/`in_progress`/`completed`),
`blocks`/`blocked_by`, `created_at`/`updated_at`, plus adk-cc-specific
`tenant_id`/`session_id`.

**Storage (`tasks/storage.py`).** Default is `JsonFileTaskStorage`:
one JSON file per task at `<root>/<tenant_id>/<session_id>/<task_id>.json`.
Root is `~/.adk-cc/tasks/` (override via `ADK_CC_TASKS_DIR`). Writes
go through `filelock.FileLock` for multi-worker uvicorn safety,
wrapped in `asyncio.to_thread` so they don't block the event loop.
Mirrors upstream's per-task JSON layout (`src/utils/tasks.ts:229`).
`InMemoryTaskStorage` remains for tests. `TaskRunner` is now a thin
storage facade (no asyncio worker pool).

**Reminder injection (`plugins/task_reminder.py`).** Upstream emits a
periodic `task_reminder` attachment listing the active tasks
(`src/utils/attachments.ts:3395-3432` + `messages.ts:3680-3699`).
adk-cc ports this as `TaskReminderPlugin.before_model_callback`. Fires
when both:

- Assistant turns since last `task_create`/`task_update` ≥
  `ADK_CC_TASK_REMINDER_TURNS_SINCE_WRITE` (default 10)
- Assistant turns since last reminder ≥
  `ADK_CC_TASK_REMINDER_TURNS_BETWEEN` (default 10)

When triggered, reads the active task list from disk and appends a
`<system-reminder>` block to `llm_request.config.system_instruction`.
Reminder text mirrors upstream verbatim with tool names rewritten to
`task_create`/`task_update`. Skips read-only specialists AND skips when
`permission_mode == "plan"` (task tools are filtered out there, so
reminding about them would just waste context). Last firing tracked in
`state["task_reminder_last_invocation_id"]` so the cooldown counter can
locate it on subsequent turns.

The plugin is registered in both `agent.py`'s `App.plugins` and
`service/server.py:build_plugins()`. Final production order:
`[Audit, Tenancy, Permission, Quota, PlanModeReminder, TaskReminder, ToolCallValidator]`.

## 7. Tool-call validator (runtime safety net)

ADK's tool-dispatch flow (`google/adk/flows/llm_flows/functions.py:489-504`) raises `ValueError` when a function_call names a tool not in the agent's `tools_dict`. That error is offered to plugins via `on_tool_error_callback`; if no plugin intervenes, ADK re-raises and the run aborts.

`ToolCallValidatorPlugin` intervenes for that specific error: it returns a structured `function_response` listing the bad tool name, the args that were attempted, the actually-available tools, and a `<system-reminder>` hint. The hint distinguishes "tool absent from this agent" from "tool filtered by plan-mode policy" — in the latter case it points the model to `exit_plan_mode` rather than a futile transfer.

The motivating failure: prompt drift causes the model to call a tool the agent doesn't have (e.g. `run_bash` from `Explore`, or `write_file` while in plan mode). Without the plugin the run aborts with a stack trace; with the plugin the model receives a corrective tool result and self-corrects on the next iteration. The plugin is registered in both `agent.py`'s `App.plugins` and `service/server.py:build_plugins()`.

## 8. What ADK does for us

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
