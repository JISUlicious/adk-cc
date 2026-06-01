"""Permission plugin — the integration point with ADK's plugin chain.

Registered on `Runner(plugins=[...])`. Runs `before_tool_callback` for
every tool call across every agent.

Layer boundary (read this before adding "deny" logic here):
  This is the CONFIRMATION + mode layer, plus a subject-BLIND guardrail
  deny-list. It answers *"is this operation forbidden for anyone, and does
  it need human confirmation?"* — NOT *"may this subject do it?"*. Its DENY
  rules are an intrinsic-danger blocklist (à la Claude Code's
  `permissions.deny`): subject-blind, default-on, keyed on tool + one arg.
  Real, subject-aware AUTHORIZATION (roles/scopes/tenant/ownership) is a
  *separate* concern owned by AuthzPlugin (`plugins/authz.py`), which runs
  BEFORE this plugin and ignores permission modes. The two layers are
  complementary — a guardrail blocklist is not a substitute for authZ, and
  authZ does not replace the guardrail. Do not move one into the other.

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

import logging
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..permissions.broadening import compute_allow_always_rule_contents
from ..permissions.confirmation import allow_once_always_deny_prompt, extract_subject
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
from .audit import emit_confirmation_resume, emit_state_mutation

_log = logging.getLogger(__name__)


# Session-state keys for runtime-injected ALLOW rules. The first lives
# under the session record (`state["adk_cc_allow_rules"]`) so it scopes
# to one session; the second uses ADK's `user:` prefix to persist
# across all of the same user's future sessions. Both are lists of
# `PermissionRule.model_dump(mode="json")` dicts so they round-trip
# cleanly through the session DB serializer.
_SESSION_ALLOW_STATE_KEY = "adk_cc_allow_rules"
_USER_ALLOW_STATE_KEY = "user:adk_cc_allow_rules"


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


def _read_persist_toggle(confirmation: Any) -> bool:
    """True only when the operator deliberately ticked the
    "Persist across sessions" box on the confirmation form. Missing /
    non-dict / wrong type all collapse to False (per-session scope)."""
    payload = getattr(confirmation, "payload", None)
    if not isinstance(payload, dict):
        return False
    return payload.get("persist_across_sessions") is True


def _load_state_rules(tool_context: ToolContext) -> list[PermissionRule]:
    """Load runtime ALLOW rules from session state. Reads both the
    per-session key and the per-user key; rules are returned in the
    order the operator added them. Malformed entries are skipped (a
    broken stash entry shouldn't block the whole turn)."""
    rules: list[PermissionRule] = []
    for key in (_SESSION_ALLOW_STATE_KEY, _USER_ALLOW_STATE_KEY):
        try:
            raw = tool_context.state.get(key) or []
        except Exception:
            continue
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                rules.append(PermissionRule.model_validate(item))
            except Exception:
                continue
    return rules


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
        # Merge the static (POLICY/USER/PROJECT) hierarchy with any
        # state-backed allow rules added at runtime via "Allow always".
        # SESSION-scope rules live in `state["adk_cc_allow_rules"]`;
        # USER-scope rules live in `state["user:adk_cc_allow_rules"]`
        # (ADK's `user:` prefix persists across the same user's future
        # sessions when a real session DB is configured). Both go into
        # the SESSION layer of the merged hierarchy — the layer is
        # priority-bottom, so operator-declared POLICY/USER/PROJECT
        # rules still win on conflict.
        effective = self._effective_settings(tool_context)
        decision = decide(
            tool=tool, args=tool_args, mode=mode, settings=effective
        )

        if decision.behavior == "deny":
            # `error` key carries the human-readable reason so frontends
            # that drive their status display off key-presence (see
            # `web/src/components/ToolCard.tsx::deriveStatus`) classify
            # this as an error without needing to know
            # `permission_denied` is failure semantics.
            return {
                "status": "permission_denied",
                "error": decision.reason,
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
                if _log.isEnabledFor(logging.DEBUG):
                    _log.debug(
                        "confirmation received tool=%s chose_id=%s confirmed=%s",
                        tool.meta.name,
                        chose_id,
                        getattr(confirmation, "confirmed", None),
                        extra={
                            "tool_name": tool.meta.name,
                            "chose_id": chose_id,
                            "confirmed": getattr(confirmation, "confirmed", None),
                        },
                    )
                emit_confirmation_resume(
                    tool_name=tool.meta.name,
                    chose_id=chose_id,
                    confirmed=getattr(confirmation, "confirmed", None),
                    function_call_id=getattr(tool_context, "function_call_id", None),
                    ctx=tool_context,
                )
                if chose_id in ("allow", "allow_once"):
                    return None  # let the tool run
                if chose_id == "allow_always":
                    persist = _read_persist_toggle(confirmation)
                    self._add_session_allow(
                        tool,
                        tool_args,
                        tool_context,
                        persist_across_sessions=persist,
                    )
                    return None  # let the tool run + skip future re-asks
                if chose_id is None and getattr(confirmation, "confirmed", False):
                    return None  # legacy back-compat path (bundled `adk web` UI)
                return {
                    "status": "permission_denied_by_user",
                    "error": "User declined the confirmation prompt.",
                    "reason": "User declined the confirmation prompt.",
                }

            # First invocation: surface a HITL pause. Tool calls without a
            # function_call_id (rare; some test contexts) skip without
            # erroring.
            if tool_context.function_call_id:
                # Include the tool's rule key (e.g. command for run_bash,
                # path for write_file) in the prompt title so the operator
                # can tell concurrent prompts apart when the model emits
                # multiple gated calls in one turn.
                subject = extract_subject(tool.meta.name, tool_args)
                # Show the broadened pattern in the Allow always
                # description so the operator knows the scope they're
                # approving (e.g. `pip install *` instead of vague
                # "this exact operation"). `compute_allow_always_rule_contents`
                # returns [literal] OR [literal, broadened] — the
                # broadened entry is what they'd actually be approving
                # beyond the literal re-run.
                contents = compute_allow_always_rule_contents(
                    tool.meta.name, tool_args
                )
                preview = contents[-1] if len(contents) >= 2 else None
                prompt = allow_once_always_deny_prompt(
                    tool.meta.name,
                    decision.reason,
                    subject=subject,
                    allow_always_preview=preview,
                )
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

    def _add_session_allow(
        self,
        tool: AdkCcTool,
        args: dict,
        tool_context: ToolContext,
        *,
        persist_across_sessions: bool = False,
    ) -> None:
        """Inject ALLOW rule(s) for the (tool, rule key) pair.

        `compute_allow_always_rule_contents` decides how broad the
        stored rule(s) are:

          - `run_bash` → typically TWO rules: the literal command
            (catches exact re-run) plus a broadened pattern via
            per-binary prefix heuristics (e.g. `pip install pandas`
            also writes `pip install *`). Compound commands like
            `cd foo && pytest` broaden each segment. See
            `adk_cc/permissions/broadening.py`.
          - Path tools (`read_file`/`write_file`/`edit_file`/`grep`/
            `glob_files`) → ONE rule, the literal path. Workspace-
            anchored broadening is a separate follow-up PR.
          - Unknown tool → ONE rule with `rule_content=None`
            (matches any args for that tool).

        Storage:
          - default → `state["adk_cc_allow_rules"]` (per-session,
            durable across agent restart when a session DB is
            configured).
          - `persist_across_sessions=True` →
            `state["user:adk_cc_allow_rules"]` (the `user:` prefix
            tells ADK to persist under the user record so the rule
            survives across the same user's future sessions).

        State-backed rules are loaded by `_effective_settings` on
        every `decide` call.
        """
        tool_name = tool.meta.name
        contents = compute_allow_always_rule_contents(tool_name, args)

        key = _USER_ALLOW_STATE_KEY if persist_across_sessions else _SESSION_ALLOW_STATE_KEY
        existing = list(tool_context.state.get(key) or [])
        added: list[dict] = []
        for content in contents:
            rule = PermissionRule(
                source=RuleSource.SESSION,
                behavior=RuleBehavior.ALLOW,
                tool_name=tool_name,
                # Empty-string contents come from the unknown-tool
                # fallback in the helper — translate to None so the
                # engine's "matches any args" path fires.
                rule_content=content if content else None,
            )
            dumped = rule.model_dump(mode="json")
            existing.append(dumped)
            added.append(dumped)
        tool_context.state[key] = existing
        # State mutation log — load-bearing for debugging "why did my
        # Allow always not stick". Captures both the scope (session
        # vs user) and the exact rule_content strings (literal +
        # broadened) so it pairs naturally with the broadening
        # heuristics in `compute_allow_always_rule_contents`.
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug(
                "state_mutation key=%s tool=%s added_rules=%s persist=%s",
                key,
                tool_name,
                [r.get("rule_content") for r in added],
                persist_across_sessions,
                extra={
                    "mutation_type": "allow_rule_added",
                    "state_key": key,
                    "tool_name": tool_name,
                    "rule_contents": [r.get("rule_content") for r in added],
                    "persist_across_sessions": persist_across_sessions,
                },
            )
        emit_state_mutation(
            mutation_type="allow_rule_added",
            state_key=key,
            details={
                "tool_name": tool_name,
                "rule_contents": [r.get("rule_content") for r in added],
                "persist_across_sessions": persist_across_sessions,
            },
            ctx=tool_context,
        )

    def _effective_settings(self, tool_context: ToolContext) -> SettingsHierarchy:
        """Merge the static hierarchy with state-backed runtime rules.

        Returns a fresh `SettingsHierarchy` rather than mutating
        `self._settings` — state-backed rules are per-context and
        must not leak into the plugin-shared instance.
        """
        state_rules = _load_state_rules(tool_context)
        if not state_rules:
            return self._settings
        return SettingsHierarchy(list(self._settings.all_rules()) + state_rules)

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
