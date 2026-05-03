# Prompts

## Lineage map

Each adk-cc prompt is a faithful port (with adaptations) of an upstream Claude Code source file. Adaptations are the same across all prompts:

- **Tool name renames**: `Read` → `read_file`, `Glob` → `glob_files`, `Grep` → `grep`, `Bash` → `run_bash`, `Edit` → `edit_file`, `Write` → `write_file`.
- **Delegation idiom**: upstream's `AgentTool` with `subagent_type=X` becomes ADK's `transfer_to_agent(agent_name='X')`.
- **Hand-off rider**: each specialist's prompt ends with a "report to the coordinator, do not address the user" block. This is unique to adk-cc — upstream's `AgentTool` returns the sub-agent's output as a tool result, so user-addressing wasn't a risk; adk-cc's `sub_agents` topology surfaces it.
- **Plan as posture, not sub-agent**: upstream's standalone Plan sub-agent has no analogue here. Planning is a posture the coordinator takes (`enter_plan_mode` → `permission_mode = "plan"`); the planning instruction lives in `PlanModeReminderPlugin.PLAN_MODE_REMINDER` and is injected dynamically at `before_model_callback` time. The text is ported from upstream's `getPlanV2SystemPrompt`.

| Prompt | Location | Upstream source |
|---|---|---|
| `EXPLORE_INSTRUCTION` | `adk_cc/prompts.py` | `src/tools/AgentTool/built-in/exploreAgent.ts` (`getExploreSystemPrompt`) |
| `VERIFY_INSTRUCTION` | `adk_cc/prompts.py` | `src/tools/AgentTool/built-in/verificationAgent.ts` (`VERIFICATION_SYSTEM_PROMPT`) |
| `COORDINATOR_INSTRUCTION` | `adk_cc/prompts.py` | composed from `src/constants/prompts.ts` (lines below) |
| `PLAN_MODE_REMINDER` | `adk_cc/plugins/plan_mode.py` | `src/tools/AgentTool/built-in/planAgent.ts` (`getPlanV2SystemPrompt`) |

## Per-prompt structure

### `EXPLORE_INSTRUCTION`

Sections: opener → `CRITICAL: READ-ONLY MODE` prohibition list → strengths → guidelines → speed/parallel-tool-call note → hand-off rider.

The READ-ONLY block exists despite the structural tool denylist because prompt-side reinforcement helps the model not invent tool calls (e.g. trying to use `run_bash` for `mkdir` — Explore doesn't have `run_bash` at all).

### `VERIFY_INSTRUCTION`

Sections: two-failure-pattern opener (verification avoidance, seduced-by-80%) → `DO NOT MODIFY THE PROJECT` (with `/tmp` allowance) → type-specific verification strategies → required baseline → "Recognize your own rationalizations" anti-pattern list → adversarial probes → output format → verdict line contract → hand-off rider.

The verdict line is the **only structural enforcement** in the entire system: the verifier's prompt produces it, the coordinator's prompt consumes it. Everything else is convention.

### `COORDINATOR_INSTRUCTION`

Composed from individual rules in `src/constants/prompts.ts`:

| Section in `COORDINATOR_INSTRUCTION` | Upstream rule | Source line |
|---|---|---|
| HARD RULE preamble (first-action whitelist; never `task_create` first) | (composed; new in adk-cc) | — |
| Doing tasks preamble | "primarily request you to perform software engineering tasks" | 222 |
| Read-before-change | "do not propose changes to code you haven't read" | 230 |
| Diagnose-before-switching | "If an approach fails, diagnose why before switching tactics" | 233 |
| Minimum-complexity | "Don't add features, refactor, or introduce abstractions beyond what the task requires" | 201–203 |
| Comments default | "Default to writing no comments. Only add one when the WHY is non-obvious" | 207 |
| Faithful reporting | "Report outcomes faithfully" | 240 |
| GATHER routing | "For broader codebase exploration and deep research, use the AgentTool with subagent_type=Explore" | 378–379 |
| PLAN routing | (composed; describes `enter_plan_mode` posture) | — |
| ACT routing | (composed; trivial — `write_file`/`edit_file`/`run_bash`) | — |
| TRACK routing (task tools as ACT-time checklist) | upstream task-tool guidance, paraphrased | — |
| Executing actions with care | full `getActionsSection()` block | 255–267 |
| VERIFY routing | "Before reporting a task complete, verify it actually works" + verifier-contract paragraph | 211 + 394 |
| Briefing template | (composed; new in adk-cc) | — |
| Style | "Lead with the answer or action, not the reasoning…" | 412–420 |

The HARD RULE preamble is new: it forbids `task_create` as a first action and lists the valid first-action options (read tool, Explore transfer, `enter_plan_mode`, `ask_user_question`). It exists because the smaller local model otherwise tended to fire `task_create` before any GATHER or PLAN, laying out tasks for work it didn't yet understand.

The PLAN routing rule is composed because upstream's coordinator doesn't have a unified "always plan before non-trivial changes" rule — it's implicit in upstream's `Plan` description. adk-cc makes it explicit, and routes to `enter_plan_mode` (the coordinator's planning posture) rather than to a sub-agent.

The TRACK routing rule is composed because adk-cc's task tools are pure tracking after the Stage F refactor (no execution semantics) and the model needs to understand they're a checklist kept WHILE acting — not a planning surface that runs before GATHER.

The briefing template is new: it specifies which fields a transfer's brief must include (depth for Explore, files-changed/approach/plan-path for verification). Upstream relies on the larger frontier model's judgment; adk-cc spells the structure out.

### `PLAN_MODE_REMINDER`

Lives in `adk_cc/plugins/plan_mode.py` rather than `prompts.py` because it's injected dynamically by `PlanModeReminderPlugin.before_model_callback` rather than set as the agent's static instruction. Sections:

- "YOU ARE CURRENTLY IN PLAN MODE" header + the prohibition (no edits, no shell, no task mutations).
- 4-step process (understand → explore → design → detail), ported from upstream's `getPlanV2SystemPrompt`.
- Required output: `write_plan` with a Markdown plan (title heading, problem statement, 4-step body, `### Critical Files for Implementation` section, optional slug for thread identity).
- Ending the turn: `exit_plan_mode` is the approval gate; do NOT also ask plain-text "is this ok?".

The plugin also filters write/exec/task tools out of `llm_request.tools_dict` and the function-declaration list when plan mode is active, so the model can't see (and therefore can't call) tools the prompt is telling it not to use. The reminder gives the rationale; the filter does the structural enforcement.

## Why prompt fidelity matters

The coordinator's prompt is the **routing table**: it names each specialist by its `agent.name`. If the prompt drifts from the actual agent names (`Explore`, `verification`), the coordinator emits invalid `transfer_to_agent` calls and ADK's `TransferToAgentTool` enum constraint rejects them. (`ToolCallValidatorPlugin` catches even those with a corrective response, but the right cure is keeping the prompt accurate.)

Conversely, the verifier's prompt is the **verdict producer**: if it stops emitting the literal `VERDICT: PASS|FAIL|PARTIAL` line, the coordinator's prompt has nothing to spot-check against.

Other prompts (Explore, the plan-mode reminder) are softer — degrading them mostly hurts quality rather than breaking the loop.

## Style adaptations

Upstream's prompts assume a frontier model (Claude Sonnet/Opus 4.x). adk-cc targets local models (Qwen 3.6 35B by default), which have:

- Weaker tool-use reliability — hence the explicit READ-ONLY MODE prohibition lists, even though tools are also denied structurally; hence the HARD RULE preamble in `COORDINATOR_INSTRUCTION` whitelisting valid first actions.
- Narrower instruction-following — hence the verbose adversarial-probe and rationalization-recognition lists in `VERIFY_INSTRUCTION`.
- More tendency to address the user when in doubt — hence the explicit hand-off rider on every specialist.

These adaptations are conservative additions, not removals: a frontier model running adk-cc would still behave correctly.
