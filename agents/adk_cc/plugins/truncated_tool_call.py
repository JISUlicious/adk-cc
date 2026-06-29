"""Graceful turn-level degradation for truncated tool calls (tolerant-json Tier 2).

When a model is cut off mid tool-call (it hit its output-token limit), the
arguments JSON is truncated mid-VALUE. tolerant_tool_json can't recover that
without fabricating the wrong argument, so — instead of raising, which crashes
the whole turn — it returns a marker (``TRUNCATED_TOOL_CALL_KEY``).

This plugin intercepts that marker in ``before_tool_callback`` and short-circuits
the tool with a clean error result: the tool NEVER runs with partial/empty
arguments, and the model receives a coherent "resend with complete arguments"
signal so it can retry within the same turn. Net effect: a model cutoff that
used to kill the turn ("Error in event_generator: …") becomes a soft, recoverable
tool error.

Pairs with the Tier 2 prevention work (configurable max_output_tokens +
finish_reason=MAX_TOKENS logging in models/selectable.py), which reduces how
often truncation happens in the first place.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from .tolerant_tool_json import TRUNCATED_TOOL_CALL_KEY

_log = logging.getLogger(__name__)


class TruncatedToolCallPlugin(BasePlugin):
    """Turns a truncated-tool-call marker into a clean retry error."""

    def __init__(self, name: str = "adk_cc_truncated_tool_call") -> None:
        super().__init__(name=name)

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        if isinstance(tool_args, dict) and tool_args.get(TRUNCATED_TOOL_CALL_KEY):
            name = getattr(tool, "name", "?")
            _log.warning(
                "TruncatedToolCallPlugin: %r call was truncated mid-emission "
                "(model hit its output-token limit) — returning a retry error "
                "instead of crashing the turn.",
                name,
            )
            return {
                "status": "error",
                "error": (
                    f"Your previous `{name}` tool call was cut off mid-emission "
                    "(the model reached its output-token limit), so its arguments "
                    "were incomplete and could not be parsed. Re-send the "
                    f"`{name}` call with the COMPLETE arguments. If an argument is "
                    "very large (e.g. a big file/HTML blob), split the work into "
                    "smaller calls."
                ),
            }
        return None
