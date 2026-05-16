"""Data-science agent on Google ADK 1.31.1.

Coordinator + 4 specialists, served over an LLM endpoint with no
filesystem / bash tools. The coordinator owns the loop
(explore → reason → plan → act → verify); specialists carry the
data-science surface and hand control back as soon as they're done.

Architecture
------------

  coordinator (the ONLY agent user-facing)
    │  Tools: record_plan, read_plan, mark_step_done, verify_completion
    │
    ├── loader     — load_from_registry / load_from_db_mock / load_from_file_mock
    ├── explorer   — list_datasets / describe_dataset / peek_dataset / profile_dataset
    ├── processor  — filter / aggregate / correlate / drop_na / transform / select
    └── visualizer — render_bar_chart / render_table / summarize_distribution

Every specialist has:
  - `disallow_transfer_to_parent=True` AND `disallow_transfer_to_peers=True`
    so the runtime hands control back to the coordinator automatically.
  - `after_agent_callback=_force_coordinator_continuation` to keep the
    coordinator's flow loop alive after the specialist's final message,
    so the coordinator gets one more LLM turn to synthesize / advance.

Loop enforcement lives in `StageGuardPlugin`:
  - Soft nudges (system-instruction prepend) telling the model which
    stage it's in.
  - Hard gates on `before_tool_callback`: refuses to invoke acting
    tools, transfers to `processor` / `visualizer`, or
    `verify_completion` before the prerequisite has been met.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps.app import App
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

from . import prompts
from .logging_setup import configure_logging
from .permissions import PermissionMode, SettingsHierarchy

configure_logging()

from .plugins import (
    AuditPlugin,
    ContextGuardPlugin,
    ModelIOTracePlugin,
    PermissionPlugin,
    ProjectContextPlugin,
    StageGuardPlugin,
    ToolCallValidatorPlugin,
)
from .tools import (
    AggregateDatasetTool,
    CorrelateTool,
    DescribeDatasetTool,
    DropNaTool,
    FilterDatasetTool,
    ListDatasetsTool,
    LoadFromDbMockTool,
    LoadFromFileMockTool,
    LoadFromRegistryTool,
    MarkStepDoneTool,
    PeekDatasetTool,
    ProfileDatasetTool,
    ReadPlanTool,
    RecordPlanTool,
    RenderBarChartTool,
    RenderTableTool,
    SelectColumnsTool,
    SummarizeDistributionTool,
    TransformColumnTool,
    VerifyCompletionTool,
)


# ---------- specialist handback ----------


def _force_coordinator_continuation(callback_context: Context) -> types.Content:
    """Yield a synthetic function-call event so the parent flow doesn't
    treat the specialist's final text as the turn's final response —
    keeps `base_llm_flow.run_async`'s while-loop alive for one more
    coordinator LLM call.

    The function-call name is never executed; it's a control signal,
    not a real tool dispatch.
    """
    return types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(
                    name="_handback_to_coordinator",
                    args={},
                )
            )
        ],
    )


# ---------- model config ----------

MODEL = LiteLlm(
    model=os.environ.get("ADK_CC_MODEL", "openai/Qwen3.6-35B-A3B-UD-MLX-4bit"),
    api_base=os.environ.get("ADK_CC_API_BASE", "http://localhost:18000/v1"),
    api_key=os.environ["ADK_CC_API_KEY"],
)


PERMISSION_MODE = PermissionMode(
    os.environ.get("ADK_CC_PERMISSION_MODE", PermissionMode.BYPASS_PERMISSIONS.value)
)
SETTINGS = SettingsHierarchy.empty()


# ---------- specialist sub-agents ----------

loader_agent = LlmAgent(
    name="loader",
    model=MODEL,
    description=(
        "Brings datasets into the working set from the in-memory registry, "
        "a mock DB backend, or a mock file backend. Read-only with respect "
        "to the source — only reports row counts and column names."
    ),
    instruction=prompts.LOADER_INSTRUCTION,
    tools=[
        LoadFromRegistryTool(),
        LoadFromDbMockTool(),
        LoadFromFileMockTool(),
    ],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=_force_coordinator_continuation,
)

explorer_agent = LlmAgent(
    name="explorer",
    model=MODEL,
    description=(
        "Profiles already-loaded datasets: row counts, column types, value "
        "ranges, null counts, distribution stats. Cannot modify data."
    ),
    instruction=prompts.EXPLORER_INSTRUCTION,
    tools=[
        ListDatasetsTool(),
        DescribeDatasetTool(),
        PeekDatasetTool(),
        ProfileDatasetTool(),
    ],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=_force_coordinator_continuation,
)

processor_agent = LlmAgent(
    name="processor",
    model=MODEL,
    description=(
        "Executes one ACT-stage computation per invocation: filter, "
        "aggregate, correlate, drop nulls, transform column, project "
        "columns. Returns the numeric result; never mutates the registry."
    ),
    instruction=prompts.PROCESSOR_INSTRUCTION,
    tools=[
        FilterDatasetTool(),
        AggregateDatasetTool(),
        CorrelateTool(),
        DropNaTool(),
        TransformColumnTool(),
        SelectColumnsTool(),
    ],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=_force_coordinator_continuation,
)

visualizer_agent = LlmAgent(
    name="visualizer",
    model=MODEL,
    description=(
        "Renders ASCII charts and markdown tables for the coordinator's "
        "final user-facing reply."
    ),
    instruction=prompts.VISUALIZER_INSTRUCTION,
    tools=[
        RenderBarChartTool(),
        RenderTableTool(),
        SummarizeDistributionTool(),
    ],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=_force_coordinator_continuation,
)


# ---------- coordinator (the main agent) ----------

_coordinator_tools = [
    RecordPlanTool(),
    ReadPlanTool(),
    MarkStepDoneTool(),
    VerifyCompletionTool(),
]

root_agent = LlmAgent(
    name="coordinator",
    model=MODEL,
    description=(
        "Coordinator: drives every data-science request through "
        "explore → reason → plan → act → verify. Owns user I/O; "
        "dispatches to loader / explorer / processor / visualizer."
    ),
    instruction=prompts.COORDINATOR_INSTRUCTION,
    tools=_coordinator_tools,
    sub_agents=[loader_agent, explorer_agent, processor_agent, visualizer_agent],
)


# ---------- App + plugin chain ----------

app = App(
    name="adk_cc",
    root_agent=root_agent,
    plugins=[
        # Audit must be first so it observes every tool attempt.
        AuditPlugin(),
        PermissionPlugin(SETTINGS, default_mode=PERMISSION_MODE),
        # Project context auto-load (CLAUDE.md / CONTEXT.md). Runs early
        # so its prepend lands above the stage-guard nudge.
        ProjectContextPlugin(),
        # Pushes the model through the loop. After ProjectContextPlugin
        # so the per-turn stage nudge appears AFTER the stable project
        # context block — most-stable info first, turn-volatile last.
        StageGuardPlugin(),
        # Catches "tool X not found" errors and turns them into a
        # structured corrective response. Generic; tool-set-agnostic.
        ToolCallValidatorPlugin(default_mode=PERMISSION_MODE.value),
        # Pre-flight context-length guardrail.
        ContextGuardPlugin(),
        # Raw model I/O trace. Last so it captures the final LlmRequest
        # after every other mutator has run.
        ModelIOTracePlugin(),
    ],
)
