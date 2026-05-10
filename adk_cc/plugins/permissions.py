"""Permission plugin — the integration point with ADK's plugin chain.

Registered on `Runner(plugins=[...])`. Runs `before_tool_callback` for
every tool call across every agent.

Behavior:
  - For non-AdkCcTool tools (e.g. ADK built-ins, MCP tools without a
    ToolMeta), the plugin passes through. Tighten this by listing
    expected tool classes if you want a default-deny posture.
  - For AdkCcTool subclasses, the plugin reads the active mode from
    `tool_context.state["permission_mode"]` (default DEFAULT) and runs
    the engine.
  - On `deny`, the plugin returns a structured dict that short-circuits
    the tool execution; the dict surfaces back to the LLM so it sees
    the denial and can adjust.
  - On `ask`, the plugin (a) calls `tool_context.request_confirmation()`
    when a `function_call_id` is available — letting the runtime pause
    the call for HITL — and (b) returns a structured dict so the model
    is informed even when no HITL UI is attached. Stage E will refine
    this into a proper resume flow.
  - On `allow`, the plugin returns None and the tool runs normally.
"""

from __future__ import annotations

from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..permissions.confirmation import allow_once_always_deny_prompt
from ..permissions.engine import decide
from ..permissions.modes import PermissionMode
from ..permissions.rules import (
    _RULE_KEY_EXTRACTORS,
    PermissionRule,
    RuleBehavior,
    RuleSource,
)
from ..permissions.settings import SettingsHierarchy
from ..tools.base import AdkCcTool


def _read_choice_id(confirmation: Any) -> Optional[str]:
    """Pull `chose_id` out of `ToolConfirmation.payload` if present.

    Returns the string id when the frontend submitted a structured
    response (`payload = {"chose_id": "allow" | "deny"}`); returns
    None otherwise so callers can fall back to `confirmed: bool`.
    Tolerates garbage payloads — a missing key, a non-dict, or an
    unexpected type all collapse to None rather than raising.
    """
    payload = getattr(confirmation, "payload", None)
    if not isinstance(payload, dict):
        return None
    chose = payload.get("chose_id")
    if isinstance(chose, str):
        return chose
    return None


class PermissionPlugin(BasePlugin):
    def __init__(
        self,
        settings: SettingsHierarchy,
        *,
        default_mode: PermissionMode = PermissionMode.DEFAULT,
        name: str = "adk_cc_permissions",
    ) -> None:
        super().__init__(name=name)
        self._settings = settings
        self._default_mode = default_mode

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        if not isinstance(tool, AdkCcTool):
            return None

        mode = self._mode_from_context(tool_context)
        decision = decide(
            tool=tool, args=tool_args, mode=mode, settings=self._settings
        )

        if decision.behavior == "deny":
            return {
                "status": "permission_denied",
                "reason": decision.reason,
                "matched_rule": (
                    decision.matched_rule.model_dump()
                    if decision.matched_rule
                    else None
                ),
            }

        if decision.behavior == "ask":
            # Two-call confirmation pattern (mirrors AdkCcTool.run_async):
            # the first invocation has tool_confirmation=None and asks;
            # ADK pauses the flow; user confirms (or denies) via the
            # frontend; ADK re-invokes the tool with tool_confirmation
            # populated. THIS callback fires for both calls — without
            # the check below, the second call would call decide()
            # again, get "ask" again, and re-prompt the user, looping
            # forever. Check the confirmation state first.
            confirmation = getattr(tool_context, "tool_confirmation", None)
            if confirmation is not None:
                # ADK has already gathered the user's response. Prefer the
                # structured `chose_id` from the payload; fall back to the
                # ADK-standard `confirmed: bool` so frontends that ignore
                # the payload protocol (e.g. the bundled `adk web` UI) still
                # work exactly as before.
                #
                # `allow` is the legacy two-option-prompt id; treat it as
                # `allow_once` for back-compat with the first cut of this
                # protocol.
                chose_id = _read_choice_id(confirmation)
                if chose_id in ("allow", "allow_once"):
                    return None  # let the tool run
                if chose_id == "allow_always":
                    self._add_session_allow(tool, tool_args)
                    return None  # let the tool run + skip future re-asks
                if chose_id is None and getattr(confirmation, "confirmed", False):
                    return None  # legacy back-compat path (bundled `adk web` UI)
                return {
                    "status": "permission_denied_by_user",
                    "reason": "User declined the confirmation prompt.",
                }

            # First invocation: surface a HITL pause. Tool calls without a
            # function_call_id (rare; some test contexts) skip without
            # erroring.
            if tool_context.function_call_id:
                prompt = allow_once_always_deny_prompt(tool.meta.name, decision.reason)
                tool_context.request_confirmation(
                    hint=decision.reason,            # back-compat for hint-only frontends
                    payload=prompt.model_dump(),     # structured for 3-option rendering
                )
                # CRITICAL: ADK's loop breaks when the last yielded event's
                # `is_final_response()` is True, which requires either
                # `actions.skip_summarization` or `long_running_tool_ids` to
                # be set. Setting `requested_tool_confirmations` alone is NOT
                # enough — ADK yields a separate request-confirmation event
                # (which IS final), but then yields the function_response_event
                # AFTER, and the loop checks the last yielded event. Without
                # this flag, the runner re-invokes the LLM before the user has
                # confirmed, the model sees `{"status": "needs_confirmation"}`
                # as a normal tool result, and decides to call another tool —
                # cascading confirmations queue up. AdkCcTool.run_async sets
                # this for the same reason; PermissionPlugin must too.
                tool_context.actions.skip_summarization = True
            return {
                "status": "needs_confirmation",
                "reason": decision.reason,
                "matched_rule": (
                    decision.matched_rule.model_dump()
                    if decision.matched_rule
                    else None
                ),
            }

        return None

    def _add_session_allow(self, tool: AdkCcTool, args: dict) -> None:
        """Inject a SESSION-scope ALLOW rule for the (tool, rule key) pair.

        Scope: exact rule-key match (e.g. for `run_bash`, the literal
        command string; for `write_file`, the literal path). The user
        explicitly approved THIS operation — broadening (e.g. fnmatch
        wildcards) would be unsafe. If the tool has no rule-key
        extractor, the rule omits `rule_content` and applies to all
        invocations of that tool for the session — a conservative
        fallback for custom tools.
        """
        tool_name = tool.meta.name
        extractor = _RULE_KEY_EXTRACTORS.get(tool_name)
        rule_content = extractor(args) if extractor is not None else None
        self._settings.add_session_rule(
            PermissionRule(
                source=RuleSource.SESSION,
                behavior=RuleBehavior.ALLOW,
                tool_name=tool_name,
                rule_content=rule_content,
            )
        )

    def _mode_from_context(self, ctx: ToolContext) -> PermissionMode:
        try:
            raw = ctx.state.get("permission_mode")
        except Exception:
            raw = None
        if not raw:
            return self._default_mode
        try:
            return PermissionMode(raw)
        except ValueError:
            return self._default_mode
