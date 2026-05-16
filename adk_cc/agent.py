"""Data-science coordinator on Google ADK 1.31.1.

The coordinator (main agent) owns user I/O and drives the four-stage
loop: explore → plan → act → verify. Specialists live under
`adk_cc/sub_agents/<name>/` and are imported here; each specialist
carries its own prompt and tool surface and hands control back via
`force_coordinator_continuation`.

Loop discipline lives in `StageGuardPlugin` — nudges via
`before_model_callback`, stage transitions via `after_tool_callback`.
No hard gates; the `verify_completion` rule check + `critic`
sub-agent verdict are the durable PASS/FAIL gate.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.apps.app import App
from google.adk.models.lite_llm import LiteLlm

from . import prompts
from .logging_setup import configure_logging

configure_logging()

from .plugins import (
    AuditPlugin,
    ContextGuardPlugin,
    ModelIOTracePlugin,
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
        "loader / explorer / processor / visualizer / critic."
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
        # Pushes the model through the loop via system-instruction nudges
        # + tracks stage transitions in session state.
        StageGuardPlugin(),
        # Catches "tool X not found" errors and turns them into a
        # structured corrective response so the model can self-correct.
        ToolCallValidatorPlugin(),
        # Pre-flight context-length guardrail (WARN at 75%, REJECT at 95%).
        ContextGuardPlugin(),
        # Raw model I/O trace, opt-in via ADK_CC_LOG_MODEL_IO=1. Last so
        # it captures the final LlmRequest after every other mutator.
        ModelIOTracePlugin(),
    ],
)
