# Prompts

## Lineage map

Each adk-cc prompt is a faithful port (with adaptations) of an upstream Claude Code source file. Adaptations are the same across all prompts:

- **Tool name renames**: `Read` → `read_file`, `Glob` → `glob_files`, `Grep` → `grep`, `Bash` → `run_bash`, `Edit` → `edit_file`, `Write` → `write_file`.
- **Delegation idiom**: upstream's `AgentTool` with `subagent_type=X` becomes ADK's `transfer_to_agent(agent_name='X')`.
- **Hand-off rider**: each specialist's prompt ends with a "report to the coordinator, do not address the user" block. This is unique to adk-cc — upstream's `AgentTool` returns the sub-agent's output as a tool result, so user-addressing wasn't a risk; adk-cc's `sub_agents` topology surfaces it.

| Prompt (in `adk_cc/prompts.py`) | Upstream source |
|---|---|
| `EXPLORE_INSTRUCTION` | `src/tools/AgentTool/built-in/exploreAgent.ts` (`getExploreSystemPrompt`) |
| `PLAN_INSTRUCTION` | `src/tools/AgentTool/built-in/planAgent.ts` (`getPlanV2SystemPrompt`) |
| `VERIFY_INSTRUCTION` | `src/tools/AgentTool/built-in/verificationAgent.ts` (`VERIFICATION_SYSTEM_PROMPT`) |
| `COORDINATOR_INSTRUCTION` | composed from `src/constants/prompts.ts` (lines below) |

## Per-prompt structure

### `EXPLORE_INSTRUCTION`

Sections: opener → `CRITICAL: READ-ONLY MODE` prohibition list → strengths → guidelines → speed/parallel-tool-call note → hand-off rider.

The READ-ONLY block exists despite the structural tool denylist because prompt-side reinforcement helps the model not invent tool calls (e.g. trying to use `Bash` for `mkdir`).

### `PLAN_INSTRUCTION`

Sections: opener → `CRITICAL: READ-ONLY MODE` prohibition list → 4-step process (Understand → Explore Thoroughly → Design → Detail) → required output (`### Critical Files for Implementation`) → REMEMBER block → hand-off rider.

The 4-step process is verbatim from upstream — it embeds gather (step 2) and trade-off discussion (step 3) inside Plan's own work, so calling Plan implicitly buys gathering too.

### `VERIFY_INSTRUCTION`

Sections: two-failure-pattern opener (verification avoidance, seduced-by-80%) → `DO NOT MODIFY THE PROJECT` (with `/tmp` allowance) → type-specific verification strategies → required baseline → "Recognize your own rationalizations" anti-pattern list → adversarial probes → output format → verdict line contract → hand-off rider.

The verdict line is the **only structural enforcement** in the entire system: the verifier's prompt produces it, the coordinator's prompt consumes it. Everything else is convention.

### `COORDINATOR_INSTRUCTION`

Composed from individual rules in `src/constants/prompts.ts`:

| Section in `COORDINATOR_INSTRUCTION` | Upstream rule | Source line |
|---|---|---|
| Doing tasks preamble | "primarily request you to perform software engineering tasks" | 222 |
| Read-before-change | "do not propose changes to code you haven't read" | 230 |
| Diagnose-before-switching | "If an approach fails, diagnose why before switching tactics" | 233 |
| Minimum-complexity | "Don't add features, refactor, or introduce abstractions beyond what the task requires" | 201–203 |
| Comments default | "Default to writing no comments. Only add one when the WHY is non-obvious" | 207 |
| Faithful reporting | "Report outcomes faithfully" | 240 |
| Explore routing rule | "For broader codebase exploration and deep research, use the AgentTool with subagent_type=Explore" | 378–379 |
| Plan routing rule | (composed; no single upstream line) | — |
| Executing actions with care | full `getActionsSection()` block | 255–267 |
| Verify-before-complete | "Before reporting a task complete, verify it actually works" | 211 |
| Verification gate contract | full verifier-contract paragraph | 394 |
| Briefing template | (composed; new in adk-cc) | — |
| Style | "Lead with the answer or action, not the reasoning…" | 412–420 |

The Plan routing rule is composed because upstream's coordinator doesn't have a unified "always plan before non-trivial changes" rule — it's implicit in upstream's `Plan` description. adk-cc makes it explicit so the smaller local model has clearer guidance.

The briefing template is new: it specifies which fields a transfer's brief must include (depth for Explore, scope for Plan, files-changed/approach/plan-path for verification). Upstream relies on the larger frontier model's judgment; adk-cc spells the structure out.

## Why prompt fidelity matters

The coordinator's prompt is the **routing table**: it names each specialist by its `agent.name`. If the prompt drifts from the actual agent names (`Explore`, `Plan`, `verification`), the coordinator emits invalid `transfer_to_agent` calls and ADK's `TransferToAgentTool` enum constraint rejects them.

Conversely, the verifier's prompt is the **verdict producer**: if it stops emitting the literal `VERDICT: PASS|FAIL|PARTIAL` line, the coordinator's prompt has nothing to spot-check against.

Other prompts (Explore, Plan) are softer — degrading them mostly hurts quality rather than breaking the loop.

## Style adaptations

Upstream's prompts assume a frontier model (Claude Sonnet/Opus 4.x). adk-cc targets local models (Qwen 3.6 35B by default), which have:

- Weaker tool-use reliability — hence the explicit READ-ONLY MODE prohibition lists, even though tools are also denied structurally.
- Narrower instruction-following — hence the verbose adversarial-probe and rationalization-recognition lists in `VERIFY_INSTRUCTION`.
- More tendency to address the user when in doubt — hence the explicit hand-off rider on every specialist.

These adaptations are conservative additions, not removals: a frontier model running adk-cc would still behave correctly.
