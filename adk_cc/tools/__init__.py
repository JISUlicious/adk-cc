"""Tool registry for the data-science agent variant.

The coordinator (main agent) owns only the loop-control tools —
`record_plan`, `read_plan`, `mark_step_done`, `verify_completion`.
Specialist sub-agents own the actual data-science surface, split
along stage lines:

  - EXPLORE: `loader` sub-agent (load_from_*), `explorer` sub-agent
    (list_datasets, describe_dataset, peek_dataset, profile_dataset)
  - ACT:     `processor` sub-agent (filter, aggregate, correlate,
    drop_na, transform_column, select_columns), `visualizer`
    sub-agent (render_bar_chart, render_table,
    summarize_distribution)
  - PLAN, REASON, VERIFY: prompt + state on the coordinator.

The coordinator transfers to a specialist via ADK's built-in
`transfer_to_agent` and gets control back via
`_force_coordinator_continuation` set on every specialist's
`after_agent_callback`. StageGuardPlugin nudges the model through
the loop and hard-gates the verify call.
"""

from __future__ import annotations

from .acting import AggregateDatasetTool, CorrelateTool, FilterDatasetTool
from .base import AdkCcTool, ToolMeta
from .exploration import (
    DescribeDatasetTool,
    ListDatasetsTool,
    PeekDatasetTool,
)
from .loader import (
    LoadFromDbMockTool,
    LoadFromFileMockTool,
    LoadFromRegistryTool,
)
from .planning import MarkStepDoneTool, ReadPlanTool, RecordPlanTool
from .preprocess import DropNaTool, SelectColumnsTool, TransformColumnTool
from .profile import ProfileDatasetTool
from .verification import VerifyCompletionTool
from .visualizer import (
    RenderBarChartTool,
    RenderTableTool,
    SummarizeDistributionTool,
)

__all__ = [
    "AdkCcTool",
    "ToolMeta",
    # loader specialist
    "LoadFromRegistryTool",
    "LoadFromDbMockTool",
    "LoadFromFileMockTool",
    # explorer specialist
    "ListDatasetsTool",
    "DescribeDatasetTool",
    "PeekDatasetTool",
    "ProfileDatasetTool",
    # coordinator: planning
    "RecordPlanTool",
    "ReadPlanTool",
    "MarkStepDoneTool",
    # processor specialist
    "FilterDatasetTool",
    "AggregateDatasetTool",
    "CorrelateTool",
    "DropNaTool",
    "TransformColumnTool",
    "SelectColumnsTool",
    # visualizer specialist
    "RenderBarChartTool",
    "RenderTableTool",
    "SummarizeDistributionTool",
    # coordinator: verify
    "VerifyCompletionTool",
]
