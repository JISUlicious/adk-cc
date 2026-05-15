# Bare-agent examples

Runnable demos showing that `adk_cc.plugins.*` is independent of
`adk_cc.tools.*`. Both demos drive an in-process `InMemoryRunner` with
a scripted `BaseLlm` — no external model server, API key, or network
needed.

## Why these exist

The structural-review discussion established a plugin tool-independence
invariant: every plugin must boot, register, and run without anything
from `adk_cc.tools.base` (or sibling tool modules) being on the agent's
tool list. Until this PR, `PermissionPlugin` violated that invariant
with an eager top-level `AdkCcTool` import. The fix (lazy import inside
`before_tool_callback` + `TYPE_CHECKING` guard) restored the
invariant; these demos exist to keep the regression visible.

## bare_agent.py

`LlmAgent(tools=[])` + the full plugin chain
(`AuditPlugin`, `PermissionPlugin`, `ProjectContextPlugin`,
`ContextGuardPlugin`, `ModelIOTracePlugin`). Seeds a temp project with
`CLAUDE.md`, chdirs in, runs one scripted turn.

What you should see in the output:

  - `project_context_loaded` audit event (CLAUDE.md picked up).
  - `model_request` event with `tool_count=0` (proves no tools).
  - `model_response` event with `parts=1`.

Run:

```
.venv/bin/python examples/bare_agent.py
```

## bare_agent_with_skills.py

Same shape, but adds `make_skill_toolset()` as the agent's only tool
surface. Seeds `.adk-cc/skills/greeter/SKILL.md` in the temp project
so discovery has something to find.

What you should see in the output:

  - `toolset skills: ['greeter']` — the project skill discovered.
  - `dispatch tools: ['list_skills', 'load_skill',
    'load_skill_resource', 'run_skill_script']` — the four
    SkillToolset dispatch tools wired.
  - `model_request` event with `tool_count=4` — confirms the
    dispatch tools reached the LLM request.
  - The same `project_context_loaded` / `model_response` events.

Run:

```
.venv/bin/python examples/bare_agent_with_skills.py
```

## data_workflow.py

A 5-step filter/sort/summarize pipeline driven by a scripted LLM
over five plain-ADK `FunctionTool`s. No `adk_cc.tools.*` imports
— the workflow is hosted entirely on the bare-agent chassis.

Tools (state-threaded via `tool_context.state['temp:employees']`):

  1. `load_employees()` — seeds a 6-row dataset.
  2. `filter_by_department(department)` — keeps matching rows.
  3. `filter_by_min_salary(min_salary)` — keeps rows above floor.
  4. `sort_by_salary(descending)` — sorts in place.
  5. `summarize_salary(operation)` — count/avg/min/max/sum.

User prompt:

> Find the average salary of engineering employees earning at least
> $90k, sorted from highest to lowest.

What you should see in the output:

  - `--- TOOL CALL TRAIL ---` listing the 5 `ATTEMPT` / `RESULT`
    pairs in scripted order, each with `status=ok`.
  - `--- AUDIT JSONL EVENT TYPES ---` showing
    `tool_call_attempt: 5`, `tool_call_result: 5`,
    `model_request: 6`, `model_response: 6`,
    `project_context_loaded: 1`.
  - `--- FINAL MODEL TEXT ---` with the agent's plain-text summary.

Run:

```
.venv/bin/python examples/data_workflow.py
```

## Regression tests

  - `tests/test_bare_agent_setup.py` — subprocess-boots
    `bare_agent.py` + `bare_agent_with_skills.py`, asserts plugin
    chain fires and expected audit events land.
  - `tests/test_data_workflow.py` — subprocess-boots
    `data_workflow.py`, asserts the exact tool sequence
    (ATTEMPT/RESULT interleave) and the exact audit-event counts.

Run both:

```
.venv/bin/python tests/test_bare_agent_setup.py
.venv/bin/python tests/test_data_workflow.py
```
