"""Session-state recorders shared across sub-agent tools.

Two helpers, both write to ADK's `temp:` state space (per-turn,
not persisted to long-term session record):

  - `record_load(ctx, source, name, row_count)` — loader-side log of
    what came into the working set. Read by the coordinator's
    `verify_completion` to confirm data was actually loaded.
  - `stash_result(ctx, tool_name, args, result)` — every acting /
    profiling / visualizing tool stashes its output here so the
    verifier can audit the chain that produced the final answer.

Centralized in `tools/` (not under any one sub-agent) because
loader, explorer, processor, and visualizer all need them.
"""

from __future__ import annotations

import time
from typing import Any

_LOADED_KEY = "temp:datasets_loaded"
_RESULTS_KEY = "temp:loop_results"


def record_load(ctx: Any, source: str, name: str, row_count: int) -> None:
    log = ctx.state.get(_LOADED_KEY) or []
    log.append(
        {
            "ts": time.time(),
            "source": source,
            "name": name,
            "row_count": row_count,
        }
    )
    ctx.state[_LOADED_KEY] = log


def stash_result(ctx: Any, tool_name: str, args: dict[str, Any], result: Any) -> None:
    log = ctx.state.get(_RESULTS_KEY) or []
    log.append(
        {"ts": time.time(), "tool": tool_name, "args": args, "result": result}
    )
    ctx.state[_RESULTS_KEY] = log
