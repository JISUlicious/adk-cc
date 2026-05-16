"""Sub-agent specialists for the data-science coordinator.

Each specialist lives in its own subpackage with its own
`agent.py`, `prompts.py`, and `tools/` directory. Specialists are
imported here so `adk_cc.agent` can wire them into the root agent
with a single import line per specialist.
"""

from __future__ import annotations

from .critic import critic_agent
from .explorer import explorer_agent
from .loader import loader_agent
from .processor import processor_agent
from .visualizer import visualizer_agent

__all__ = [
    "critic_agent",
    "explorer_agent",
    "loader_agent",
    "processor_agent",
    "visualizer_agent",
]
