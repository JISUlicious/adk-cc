"""Catch unknown-tool calls and return a corrective response so the model
self-corrects, rather than letting the error abort the turn.

ADK's tool-dispatch flow (`google/adk/flows/llm_flows/functions.py:489-504`)
raises `ValueError` when a function_call names a tool not in the agent's
tools_dict. That error is offered to plugins via `on_tool_error_callback`;
if no plugin intervenes, ADK re-raises and the run aborts.

The motivating failure: a sub-agent (e.g. `Plan`) does not have `run_bash`,
but the model called it anyway (prompt drift, or seeing the tool on the
coordinator's surface and assuming it's universal). Without this plugin,
the user sees a stack trace and the conversation gets stuck.

With this plugin, the model receives a structured `function_response` that
names what it tried, lists what is available on this agent, and tells it
to retry against an available tool or `transfer_to_agent` to an agent that
has the missing one. The next iteration produces a corrected call.
"""

from __future__ import annotations

from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

# String shared with ADK's _get_tool error template; used to scope this
# plugin to the specific failure mode it knows how to repair.
_NOT_FOUND_MARKER = "not found.\nAvailable tools:"


class ToolCallValidatorPlugin(BasePlugin):
    """Convert "tool not found" ValueErrors into corrective tool responses."""

    def __init__(self, name: str = "adk_cc_tool_call_validator") -> None:
        super().__init__(name=name)

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> Optional[dict]:
        message = str(error)
        if _NOT_FOUND_MARKER not in message:
            return None

        available = self._extract_available(message)
        agent_name = self._current_agent_name(tool_context)

        hint = (
            f"<system-reminder>\n"
            f"You called `{tool.name}`, which is NOT available in "
            f"{f'agent `{agent_name}`' if agent_name else 'this agent'}. "
            f"Available tools here: {', '.join(available) or '(none)'}.\n\n"
            f"Decide:\n"
            f"  - If one of the available tools fits, retry with that.\n"
            f"  - If you need a tool that lives on a different agent, "
            f"`transfer_to_agent(agent_name=...)` to that agent.\n"
            f"  - Do NOT retry `{tool.name}` here — it will fail again.\n"
            f"</system-reminder>"
        )
        return {
            "status": "tool_unavailable",
            "tool_name": tool.name,
            "args_attempted": tool_args,
            "available_tools": available,
            "hint": hint,
        }

    @staticmethod
    def _extract_available(message: str) -> list[str]:
        """Pull the comma-separated tool list out of ADK's error template."""
        try:
            tail = message.split(_NOT_FOUND_MARKER, 1)[1]
            tail = tail.split("\n\nPossible causes:", 1)[0]
            return [name.strip() for name in tail.split(",") if name.strip()]
        except Exception:
            return []

    @staticmethod
    def _current_agent_name(tool_context: ToolContext) -> Optional[str]:
        try:
            return tool_context._invocation_context.agent.name
        except Exception:
            return None
