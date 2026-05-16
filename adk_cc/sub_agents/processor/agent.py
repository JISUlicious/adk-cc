"""`processor` sub-agent — ACT-stage data computation."""

from __future__ import annotations

from google.adk.agents import LlmAgent

from .._shared import force_coordinator_continuation, make_specialist_model
from .prompts import PROCESSOR_INSTRUCTION
from .tools import (
    AggregateDatasetTool,
    CorrelateTool,
    DropNaTool,
    FilterDatasetTool,
    SelectColumnsTool,
    TransformColumnTool,
)


processor_agent = LlmAgent(
    name="processor",
    model=make_specialist_model(),
    description=(
        "Executes one ACT-stage computation per invocation: filter, "
        "aggregate, correlate, drop nulls, transform column, project "
        "columns. Returns the numeric result; never mutates the registry."
    ),
    instruction=PROCESSOR_INSTRUCTION,
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
    after_agent_callback=force_coordinator_continuation,
)
