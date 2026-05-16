"""Coordinator-side and shared tooling.

The coordinator owns four loop-bookkeeping tools (record_plan,
read_plan, mark_step_done, verify_completion). Specialist tools live
under `adk_cc/sub_agents/<name>/tools/`. Shared infrastructure that
specialists' tools build on:

  - `base.py`        — `AdkCcTool` + `ToolMeta` framework.
  - `datasets.py`    — in-memory dataset registry.
  - `loop_state.py`  — session-state recorders (`record_load`,
                       `stash_result`) used by every specialist.
"""

from __future__ import annotations

from .base import AdkCcTool, ToolMeta
from .planning import MarkStepDoneTool, ReadPlanTool, RecordPlanTool
from .verification import VerifyCompletionTool

__all__ = [
    "AdkCcTool",
    "ToolMeta",
    "MarkStepDoneTool",
    "ReadPlanTool",
    "RecordPlanTool",
    "VerifyCompletionTool",
]
