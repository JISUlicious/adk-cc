"""Data-science coordinator on Google ADK 1.31.1.

The coordinator (main agent) owns user I/O and drives the four-stage
loop: explore → plan → act → verify. Specialists live under
`adk_cc/sub_agents/<name>/` and are imported here; each specialist
carries its own prompt and tool surface and hands control back via
`force_coordinator_continuation`.

Loop enforcement lives in `StageGuardPlugin`:
  - Soft nudges (system-instruction prepend) telling the model which
    stage it's in.
  - Hard gates on `before_tool_callback`: refuses acting tools and
    transfers to `processor` / `visualizer` before a plan is
    recorded, and refuses `verify_completion` until every plan step
    is `status=done`.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.apps.app import App
from google.adk.models.lite_llm import LiteLlm

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
from .sub_agents import (
    critic_agent,
    explorer_agent,
    loader_agent,
    processor_agent,
    visualizer_agent,
)
from .tools import (
    ReadPlanTool,
    RecordPlanTool,
    VerifyCompletionTool,
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


# ---------- coordinator (the main agent) ----------

_coordinator_tools = [
    RecordPlanTool(),
    ReadPlanTool(),
    VerifyCompletionTool(),
]

root_agent = LlmAgent(
    name="coordinator",
    model=MODEL,
    description=(
        "Coordinator: drives every data-science request through "
        "explore → plan → act → verify. Owns user I/O; dispatches to "
        "loader / explorer / processor / visualizer."
    ),
    instruction=prompts.COORDINATOR_INSTRUCTION,
    tools=_coordinator_tools,
    sub_agents=[
        loader_agent,
        explorer_agent,
        processor_agent,
        visualizer_agent,
        critic_agent,
    ],
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
        # structured corrective response.
        ToolCallValidatorPlugin(default_mode=PERMISSION_MODE.value),
        # Pre-flight context-length guardrail.
        ContextGuardPlugin(),
        # Raw model I/O trace. Last so it captures the final LlmRequest
        # after every other mutator has run.
        ModelIOTracePlugin(),
    ],
)
