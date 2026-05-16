"""`loader` sub-agent — EXPLORE-stage data ingestion."""

from __future__ import annotations

from google.adk.agents import LlmAgent

from .._shared import force_coordinator_continuation, make_specialist_model
from .prompts import LOADER_INSTRUCTION
from .tools import (
    LoadFromDbMockTool,
    LoadFromFileMockTool,
    LoadFromRegistryTool,
)


loader_agent = LlmAgent(
    name="loader",
    model=make_specialist_model(),
    description=(
        "Brings datasets into the working set from the in-memory registry, "
        "a mock DB backend, or a mock file backend. Read-only with respect "
        "to the source — only reports row counts and column names."
    ),
    instruction=LOADER_INSTRUCTION,
    tools=[
        LoadFromRegistryTool(),
        LoadFromDbMockTool(),
        LoadFromFileMockTool(),
    ],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=force_coordinator_continuation,
)
