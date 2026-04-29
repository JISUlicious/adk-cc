"""Tool registry for adk-cc.

Stage A: each tool is an `AdkCcTool` subclass with a `ToolMeta` describing
its policy-relevant flags. `agent.py` imports the classes directly and
instantiates one per agent's tool surface.
"""

from __future__ import annotations

from .bash import BashTool
from .base import AdkCcTool, ToolMeta
from .edit_file import EditFileTool
from .glob_files import GlobFilesTool
from .grep import GrepTool
from .read_file import ReadFileTool
from .write_file import WriteFileTool

__all__ = [
    "AdkCcTool",
    "ToolMeta",
    "BashTool",
    "EditFileTool",
    "GlobFilesTool",
    "GrepTool",
    "ReadFileTool",
    "WriteFileTool",
]
