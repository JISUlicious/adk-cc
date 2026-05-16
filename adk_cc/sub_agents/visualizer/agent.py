"""`visualizer` sub-agent — ACT-stage rendering for the final reply."""

from __future__ import annotations

from google.adk.agents import LlmAgent

from .._shared import force_coordinator_continuation, make_specialist_model
from .prompts import VISUALIZER_INSTRUCTION
from .tools import (
    RenderBarChartTool,
    RenderTableTool,
    SummarizeDistributionTool,
)


visualizer_agent = LlmAgent(
    name="visualizer",
    model=make_specialist_model(),
    description=(
        "Renders ASCII charts and markdown tables for the coordinator's "
        "final user-facing reply."
    ),
    instruction=VISUALIZER_INSTRUCTION,
    tools=[
        RenderBarChartTool(),
        RenderTableTool(),
        SummarizeDistributionTool(),
    ],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=force_coordinator_continuation,
)
