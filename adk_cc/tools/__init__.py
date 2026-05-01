"""Tool registry for adk-cc.

Stage A: each tool is an `AdkCcTool` subclass with a `ToolMeta` describing
its policy-relevant flags. `agent.py` imports the classes directly and
instantiates one per agent's tool surface.
"""

from __future__ import annotations

from .ask_user_question import AskUserQuestionTool
from .bash import BashTool
from .base import AdkCcTool, ToolMeta
from .edit_file import EditFileTool
from .enter_plan_mode import EnterPlanModeTool
from .exit_plan_mode import ExitPlanModeTool
from .glob_files import GlobFilesTool
from .grep import GrepTool
from .mcp import make_mcp_toolset
from .read_current_plan import ReadCurrentPlanTool
from .read_file import ReadFileTool
from .skills import make_skill_toolset
from .task import (
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskStopTool,
    TaskUpdateTool,
)
from .web_fetch import WebFetchTool
from .write_file import WriteFileTool
from .write_plan import WritePlanTool

__all__ = [
    "AdkCcTool",
    "ToolMeta",
    "AskUserQuestionTool",
    "BashTool",
    "EditFileTool",
    "EnterPlanModeTool",
    "ExitPlanModeTool",
    "GlobFilesTool",
    "GrepTool",
    "ReadCurrentPlanTool",
    "ReadFileTool",
    "TaskCreateTool",
    "TaskGetTool",
    "TaskListTool",
    "TaskStopTool",
    "TaskUpdateTool",
    "WebFetchTool",
    "WriteFileTool",
    "WritePlanTool",
    "make_mcp_toolset",
    "make_skill_toolset",
]
