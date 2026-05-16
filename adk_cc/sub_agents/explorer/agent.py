"""`explorer` sub-agent — EXPLORE-stage dataset profiling."""

from __future__ import annotations

from google.adk.agents import LlmAgent

from .._shared import force_coordinator_continuation, make_specialist_model
from .prompts import EXPLORER_INSTRUCTION
from .tools import (
    DescribeDatasetTool,
    ListDatasetsTool,
    PeekDatasetTool,
    ProfileDatasetTool,
)


explorer_agent = LlmAgent(
    name="explorer",
    model=make_specialist_model(),
    description=(
        "Profiles already-loaded datasets: row counts, column types, value "
        "ranges, null counts, distribution stats. Cannot modify data."
    ),
    instruction=EXPLORER_INSTRUCTION,
    tools=[
        ListDatasetsTool(),
        DescribeDatasetTool(),
        PeekDatasetTool(),
        ProfileDatasetTool(),
    ],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=force_coordinator_continuation,
)
