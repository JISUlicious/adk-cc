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

## Regression test

`tests/test_bare_agent_setup.py` boots both demos via subprocess and
asserts the expected audit events landed. Run it as part of the unit
sweep:

```
.venv/bin/python tests/test_bare_agent_setup.py
```
