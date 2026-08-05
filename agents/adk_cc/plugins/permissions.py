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

import glob
import logging
import os
import re
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..permissions.broadening import compute_allow_always_rule_contents
from ..permissions.confirmation import (
    allow_once_always_deny_prompt,
    extract_subject,
    grant_scope_prompt,
)
from ..permissions.engine import decide
from ..permissions.modes import PermissionMode
from ..permissions.protected import classify_path
from ..permissions.rules import (
    _PATH_TOOLS,
    _RULE_KEY_EXTRACTORS,
    PermissionRule,
    RuleBehavior,
    RuleSource,
    _resolve_against_workspace,
    rule_matches,
)
from ..permissions.settings import SettingsHierarchy
from ..tools.base import AdkCcTool
from .audit import emit_confirmation_resume, emit_state_mutation

_log = logging.getLogger(__name__)

# Sentinel: the desktop scope-expansion gate returns this to mean "not
# applicable — fall through to the normal permission decision".
_CONTINUE = object()


# Session-state keys for runtime-injected ALLOW rules. The first lives
# under the session record (`state["adk_cc_allow_rules"]`) so it scopes
# to one session; the second uses ADK's `user:` prefix to persist
# across all of the same user's future sessions. Both are lists of
# `PermissionRule.model_dump(mode="json")` dicts so they round-trip
# cleanly through the session DB serializer.
# ADK's own skill-script tool, gated here by NAME because it is not an
# AdkCcTool and so never carried a ToolMeta.
SKILL_SCRIPT_TOOL = "run_skill_script"

# How much of a script to put in front of the user. Enough to see what it does
# and to spot the obvious ("curl … | sh", a write to $HOME); not so much that
# the buttons scroll off the card. The rest is one `load_skill_resource` away.
_SCRIPT_PREVIEW_LINES = 40
_SCRIPT_PREVIEW_CHARS = 2400


def skill_script_preview(tool: Any, args: dict) -> str:
    """The script's own source, for the confirmation card.

    "Do you want to run openscad/scripts/render.sh?" is unanswerable without
    it — the whole point of asking is that the user can look. Read from the
    loaded skill (the same bytes the launcher will materialise), never from
    disk, so what is shown is what will run.
    """
    skill_name = str(args.get("skill_name") or "")
    rel = str(args.get("file_path") or "")
    body = ""
    try:
        toolset = getattr(tool, "_toolset", None)
        skill = toolset._get_skill(skill_name) if toolset is not None else None
        if skill is not None:
            want = rel[len("scripts/"):] if rel.startswith("scripts/") else rel
            scr = skill.resources.get_script(want)
            src = getattr(scr, "src", None)
            if isinstance(src, bytes):
                body = f"(binary, {len(src)} bytes)"
            elif isinstance(src, str):
                body = src
    except Exception:  # noqa: BLE001 — a preview that fails must not block the ask
        body = ""
    if not body:
        return f"{skill_name} / {rel}\n(could not read the script to show it)"

    lines = body.splitlines()
    shown = "\n".join(lines[:_SCRIPT_PREVIEW_LINES])[:_SCRIPT_PREVIEW_CHARS]
    more = ""
    if len(lines) > _SCRIPT_PREVIEW_LINES or len(shown) < len(body):
        more = f"\n… ({len(lines)} lines, {len(body)} bytes in total)"
    return f"{skill_name} / {rel}\n\n{shown}{more}"


# A PROJECT skill's scripts are also plain files in the workspace, so the
# skill gate is reachable a second way: `bash .adk-cc/skills/x/scripts/y.sh`.
# Measured live — when the gated tool failed, the model read the script and ran
# it through run_bash, in-scope and unflagged, and it wrote to $HOME. Matching
# the invocation path lets both channels share ONE rule key (skill:file), so a
# grant made on either covers the other.
# Two shapes reach the shell, and BOTH are the same skill script:
#   .adk-cc/skills/<name>/scripts/x.py                    (the source)
#   .adk-cc/skill-runtime/<name>/<digest>/scripts/x.py    (materialised)
# The runtime form is where a script actually lives when it runs, and it was
# unmatched — so it fell through to the generic bash gate, whose rule key is
# the command string INCLUDING the digest. Every skill edit changes the
# digest, so an "allow always" stopped applying and the user was asked again
# (reported live: "running skills scripts keep falling into user
# confirmation, repeatedly"). Both shapes now yield the SAME
# `<skill>:<scripts/...>` key, so one grant survives skill edits.
_BASH_SKILL_SCRIPT_RE = re.compile(
    r"(?:^|[\s;&|(`'\"=])(?:\./)?(?:[\w./-]*/)?"
    r"\.(?:adk-cc|agents)/(?:"
    r"skills/([\w.-]+)/(scripts/[\w./-]+)"
    r"|skill-runtime/([\w.-]+)/[\w.-]+/(scripts/[\w./-]+)"
    r")")


def _bash_skill_script(command: str):
    """(skill_name, file_path) when `command` runs a skill script, else None."""
    m = _BASH_SKILL_SCRIPT_RE.search(command or "")
    if not m:
        return None
    name = m.group(1) or m.group(3)
    rel = m.group(2) or m.group(4)
    return (name, rel) if name and rel else None

def _pending_deps(tool: Any, args: dict) -> list[str]:
    """The Python packages the launcher will install on this skill's first
    run — shown BEFORE the click, because an install is a side effect the
    script's source alone does not reveal."""
    try:
        from ..tools.skill_deps import collect_requirements

        toolset = getattr(tool, "_toolset", None)
        skill = (toolset._get_skill(str(args.get("skill_name") or ""))
                 if toolset is not None else None)
        return collect_requirements(skill) if skill is not None else []
    except Exception:  # noqa: BLE001 — the card must render regardless
        return []


def _preview_from_workspace(tool_context: Any, workspace_root: Optional[str],
                            args: dict) -> str:
    """The script's source for the BASH route, read from the workspace file —
    which for this route is exactly what will run."""
    skill = str(args.get("skill_name") or "")
    rel = str(args.get("file_path") or "")
    for base in (".adk-cc", ".agents"):
        try:
            path = os.path.join(workspace_root or "", base, "skills", skill, rel)
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    body = fh.read(_SCRIPT_PREVIEW_CHARS + 1)
                lines = body.splitlines()
                shown = "\n".join(lines[:_SCRIPT_PREVIEW_LINES])[:_SCRIPT_PREVIEW_CHARS]
                more = " …" if len(body) > _SCRIPT_PREVIEW_CHARS else ""
                return f"{skill} / {rel} (invoked via run_bash)\n\n{shown}{more}"
        except OSError:
            continue
    return f"{skill} / {rel} (invoked via run_bash; could not read the script)"


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


def _in_subagent(tool_context: ToolContext) -> bool:
    """Is this tool call running inside a spawned sub-agent's session?

    A sub-agent has no human: its events never reach the UI, so a
    confirmation raised there has no one to answer it — the fan-out would
    hang. Every would-ask below becomes a structured deny instead, telling
    the coordinator to do that step itself.
    """
    try:
        return bool(tool_context.state.get("subagent"))
    except Exception:  # noqa: BLE001
        return False


def _subagent_deny(reason: str) -> dict:
    return {
        "status": "permission_denied",
        "error": ("this operation needs user confirmation, and no user is "
                  "reachable from a sub-agent — report the finding without it, "
                  "or the coordinator must perform this step itself"),
        "reason": reason,
    }


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
        # `run_skill_script` is one of ADK's own tools, so it never reached the
        # AdkCcTool gate below — and it is the widest hole there is: the script
        # is third-party code, it runs with the session's authority, and
        # nothing mediates what it does INSIDE. Measured live: a published
        # skill's script created ~/openscad-projects and wrote there, silently,
        # while the agent's own write_file to that same path was stopped by the
        # floor and asked.
        if getattr(tool, "name", "") == SKILL_SCRIPT_TOOL:
            return await self._gate_skill_script(tool, tool_args, tool_context)

        # The same gate for the bash route into a project skill's scripts.
        # Read-only commands (cat/grep of a script) stay free — the gate is
        # about EXECUTING third-party code, not reading it.
        if isinstance(tool, AdkCcTool) and tool.meta.name == "run_bash":
            m = _bash_skill_script(str(tool_args.get("command") or ""))
            if m is not None:
                from ..tools.bash.readonly import is_read_only_command

                if not is_read_only_command(str(tool_args.get("command") or "")):
                    gate = await self._gate_skill_script(
                        tool, {"skill_name": m[0], "file_path": m[1]},
                        tool_context, via_bash=True)
                    if gate is not None:
                        return gate
                    # Granted or allowed — fall through to the NORMAL bash
                    # decision; scope and danger checks still apply.

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
        # Workspace root anchors path-tool rules: `decide` resolves relative
        # path args against it when matching, and "Allow always" broadens to
        # `<root>/*`. In desktop in-place mode this is the bound project root.
        workspace_root = self._workspace_root(tool_context)

        # Desktop scope-expansion gate: a path tool targeting OUTSIDE the project
        # ∪ granted directories prompts to grant (Allow folder / once / deny)
        # instead of hitting the sandbox hard-deny. Handles both HITL calls;
        # returns _CONTINUE when not applicable (non-desktop, non-path tool,
        # in-scope, secret material, or a non-scope confirmation).
        gate = self._scope_gate(
            tool, tool_args, tool_context, mode=mode, workspace_root=workspace_root
        )
        if gate is not _CONTINUE:
            return gate

        decision = decide(
            tool=tool, args=tool_args, mode=mode, settings=effective,
            workspace_root=workspace_root,
            cmd_out_of_scope=self._bash_out_of_scope(
                tool, tool_args, tool_context, workspace_root
            ),
            remote_home=self._remote_home(tool_context),
        )

        if decision.behavior == "deny":
            return self._deny_result(decision)

        if decision.behavior == "ask":
            if _in_subagent(tool_context):
                return _subagent_deny(decision.reason)
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
                        workspace_root=workspace_root,  # reuse the local (line ~172)
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
                    tool.meta.name, tool_args, workspace_root=workspace_root
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
                # Same pause contract as the skill gate (P1): without it the
                # model read "needs_confirmation" as a refusal and invented a
                # detour. One live incident showed it rewriting a gated
                # `rm ~/…` into an in-workspace variant to slip past the ask.
                "is_pause_not_denial": True,
                "next_step": (
                    "Wait — this is not a refusal. The user is being asked to "
                    "approve this exact action, and their answer resumes it "
                    "automatically. Do not restate the action another way to "
                    "avoid the confirmation. Say you are waiting for approval "
                    "and end your turn."),
                "matched_rule": (
                    decision.matched_rule.model_dump()
                    if decision.matched_rule
                    else None
                ),
            }

        return None

    # ---------------------------------------------------------------- skills
    async def _gate_skill_script(
        self,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        via_bash: bool = False,
    ) -> Optional[dict]:
        """Ask once before running a skill's script, showing what it contains.

        Same three options as the bash gate — Allow once / Allow always / Deny
        — and the same rule storage, so one list of grants covers both. What
        differs is the payload: the script's SOURCE goes in the detail, because
        "do you trust openscad/scripts/render.sh" is not a question anyone can
        answer without reading it.

        Why this asks even under bypassPermissions: the mode says the user
        trusts THE AGENT's judgement about its own actions. A skill script is
        somebody else's code, and running it is closer to `curl | sh` than to
        an edit the agent decided to make — the dangerous-command gate takes
        the same line.
        """
        key = f"{tool_args.get('skill_name', '')}:{tool_args.get('file_path', '')}"

        # Already granted (this session, or persisted for this user)?
        effective = self._effective_settings(tool_context)
        for rule in effective.all_rules():
            if (rule.behavior is RuleBehavior.ALLOW
                    and rule_matches(rule, SKILL_SCRIPT_TOOL, tool_args)):
                return None

        confirmation = getattr(tool_context, "tool_confirmation", None)
        if confirmation is not None:
            chose_id = _read_choice_id(confirmation)
            emit_confirmation_resume(
                tool_name=SKILL_SCRIPT_TOOL,
                chose_id=chose_id,
                confirmed=getattr(confirmation, "confirmed", None),
                function_call_id=getattr(tool_context, "function_call_id", None),
                ctx=tool_context,
            )
            if chose_id in ("allow", "allow_once"):
                return None
            if chose_id == "allow_always":
                self._add_session_allow(
                    SKILL_SCRIPT_TOOL, tool_args, tool_context,
                    persist_across_sessions=_read_persist_toggle(confirmation),
                )
                return None
            if chose_id is None and getattr(confirmation, "confirmed", False):
                return None
            return {
                "status": "permission_denied_by_user",
                "error": "User declined to run this skill script.",
                "reason": "User declined to run this skill script.",
            }

        if _in_subagent(tool_context):
            return _subagent_deny("skill scripts cannot be confirmed from a "
                                  "sub-agent")
        if not tool_context.function_call_id:
            return None      # no HITL channel (some test contexts) — don't block

        reason = (
            "a skill's script is third-party code and runs with this session's "
            "access; adk-cc does not mediate what it does once started"
        )
        if via_bash:
            # No install line here: the bash route executes the file directly,
            # so the launcher's lazy install never runs on it.
            preview = _preview_from_workspace(
                tool_context, self._workspace_root(tool_context), tool_args)
        else:
            preview = skill_script_preview(tool, tool_args)
            deps = _pending_deps(tool, tool_args)
            if deps:
                preview += (
                    "\n\nFirst run will also install into the analysis "
                    "environment: " + ", ".join(deps))
        prompt = allow_once_always_deny_prompt(
            SKILL_SCRIPT_TOOL,
            reason + "\n\n" + preview,
            subject=key,
            allow_always_preview=f"{tool_args.get('skill_name', '')}:*",
        )
        tool_context.request_confirmation(hint=reason, payload=prompt.model_dump())
        tool_context.actions.skip_summarization = True
        # A PAUSE, not a refusal — the distinction the model could not make.
        # Reported live: it read "needs_confirmation" as a denial and announced
        # "let me run with bash", then ran the script past the gate.
        #
        # Deliberately NOT a blanket prohibition on run_bash. That would be
        # unenforceable (this is text in a tool result, not a control) and
        # wrong in general: routing a bash call at a skill script THROUGH the
        # launcher is the intended behaviour (#113 part 3), not something to
        # forbid. The real fix is that answering must actually resume this
        # call — see #114. This only stops the model inventing a detour while
        # it waits.
        return {
            "status": "needs_confirmation",
            "reason": reason,
            "is_pause_not_denial": True,
            "next_step": (
                "Wait — this is not a refusal. The user is being asked to "
                "approve this exact run, and their answer resumes it "
                "automatically; you will get the script's output. Say you are "
                "waiting for approval and end your turn."),
        }

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> Optional[dict]:
        """Consume any one-shot filesystem grant so "Allow once" applies to
        exactly one operation (desktop path tools only)."""
        if not isinstance(tool, AdkCcTool):
            return None
        from .. import deployment

        if deployment.is_desktop() and tool.meta.name in _PATH_TOOLS:
            from ..sandbox import discard_grant_once

            # Consume only THIS call's grant (not the whole list), so a sibling
            # tool call finishing first can't drop another call's "Allow once".
            resolved = self._resolved_target(tool, tool_args, tool_context)
            if resolved:
                discard_grant_once(tool_context, resolved)
        return None

    # --- Desktop scope-expansion gate ------------------------------------

    def _resolved_target(
        self, tool: AdkCcTool, args: dict, tool_context: ToolContext
    ) -> Optional[str]:
        """Resolved absolute path of a path tool's target (relative args anchored
        at the project root), or None."""
        extractor = _RULE_KEY_EXTRACTORS.get(tool.meta.name)
        if extractor is None:
            return None
        raw = extractor(args)
        if not raw:
            return None
        try:
            from ..sandbox import get_workspace

            ws = get_workspace(tool_context)
            return _resolve_against_workspace(raw, ws.abs_path)
        except Exception:
            return None

    def _in_scope(self, tool_context: ToolContext, resolved: str) -> bool:
        """True if `resolved` is inside the project ∪ granted directories (i.e.
        the sandbox fs guard would allow it)."""
        try:
            from ..sandbox import get_workspace

            return get_workspace(tool_context).fs_read_config().allows(resolved)
        except Exception as e:  # noqa: BLE001
            # Fail open to the normal flow (gate 2 still enforces) — but LOUDLY:
            # a silent fail-open here made the F6 live inconsistency
            # undiagnosable (out-of-scope writes passing with no trace).
            _log.warning(
                "scope check failed OPEN for %r (%s: %s) — treating as in-scope",
                resolved, type(e).__name__, e,
            )
            return True

    def _bash_out_of_scope(
        self, tool: AdkCcTool, args: dict, tool_context: ToolContext,
        workspace_root: Optional[str],
    ) -> bool:
        """True when a desktop run_bash command references a path OUTSIDE the
        project ∪ granted dirs — drives the engine's out-of-project write/delete
        floor (Step 1f). run_bash isn't a `_PATH_TOOL`, so the scope gate skips
        it; this is the command analog. Only MUTATING commands actually hit the
        floor (reads are read-only tier and return earlier), so a `cat` of an
        out-of-scope path stays auto — this narrows the prompt to writes/deletes.
        Best-effort: shell expansion (`$HOME`, shell globs) can hide a path; the
        OS sandbox is the airtight boundary."""
        from .. import deployment

        if not deployment.is_desktop() or tool.meta.name != "run_bash":
            return False
        from ..permissions.command_safety import command_paths

        command = str((args or {}).get("command") or "")
        for raw in command_paths(command):
            resolved = _resolve_against_workspace(raw, workspace_root)
            if resolved and not self._in_scope(tool_context, resolved):
                return True
        return False

    def _deny_result(self, decision) -> dict:
        """The permission_denied dict. `error` carries the reason so frontends
        that key their status display off its presence classify it as an error."""
        return {
            "status": "permission_denied",
            "error": decision.reason,
            "reason": decision.reason,
            "matched_rule": (
                decision.matched_rule.model_dump() if decision.matched_rule else None
            ),
        }

    def _scope_gate(
        self, tool: AdkCcTool, args: dict, tool_context: ToolContext,
        *, mode: PermissionMode, workspace_root: Optional[str],
    ) -> Any:
        """Desktop-only scope-expansion for path tools. Routes through `decide()`
        so operator DENY rules, PLAN-mode blocking, the protected-path floor, and
        the audit trail are NEVER bypassed by a grant. Returns `_CONTINUE` to fall
        through to the normal flow, or a tool-result dict / None to short-circuit."""
        from .. import deployment

        if not deployment.is_desktop() or tool.meta.name not in _PATH_TOOLS:
            return _CONTINUE

        def _decide():  # fresh settings so a just-applied grant's rule is seen
            return decide(
                tool=tool, args=args, mode=mode,
                settings=self._effective_settings(tool_context),
                workspace_root=workspace_root,
                remote_home=self._remote_home(tool_context),
            )

        confirmation = getattr(tool_context, "tool_confirmation", None)
        if confirmation is not None:
            chose = _read_choice_id(confirmation)
            if chose in ("grant_folder", "grant_once"):
                if chose == "grant_folder":
                    self._apply_folder_grant(
                        tool, args, tool_context,
                        persist=_read_persist_toggle(confirmation),
                    )
                else:
                    resolved = self._resolved_target(tool, args, tool_context)
                    if resolved:
                        from ..sandbox import grant_once

                        grant_once(tool_context, resolved)
                # Grant applied → the path is now in scope. Still honor a hard DENY
                # (operator rule / plan-mode / protected-deny); the grant is the
                # confirmation for any ask.
                d = _decide()
                if d.behavior == "deny":
                    return self._deny_result(d)
                return None  # let the tool run
            if chose == "grant_deny":
                return {
                    "status": "permission_denied_by_user",
                    "error": "User declined to grant access outside the project.",
                    "reason": "User declined the scope-expansion prompt.",
                }
            return _CONTINUE  # a normal ask confirmation → main flow handles it

        # First invocation.
        resolved = self._resolved_target(tool, args, tool_context)
        if resolved is None or self._in_scope(tool_context, resolved):
            return _CONTINUE
        # Never offer a grant for something the engine would hard-DENY (deny rule,
        # plan-mode block, protected-deny) — let decide() deny it via the main flow.
        if _decide().behavior == "deny":
            return _CONTINUE
        if _in_subagent(tool_context):
            # No human to grant scope from inside a sub-agent.
            return _subagent_deny(
                f"{tool.meta.name} targets a path outside the project scope")
        if not tool_context.function_call_id:
            return _CONTINUE  # no HITL channel; gate 2 will still deny

        prot = classify_path(resolved)
        parent = os.path.dirname(resolved)
        prompt = grant_scope_prompt(
            tool.meta.name, resolved, parent,
            allow_folder=(prot != "ask"),  # protected file: no broad folder grant
        )
        tool_context.request_confirmation(
            hint="Access outside the project requires your approval.",
            payload=prompt.model_dump(),
        )
        # Without skip_summarization the runner re-invokes the LLM before the
        # user answers.
        tool_context.actions.skip_summarization = True
        return {
            "status": "needs_confirmation",
            "reason": f"{tool.meta.name} targets a path outside the project scope",
        }

    def _apply_folder_grant(
        self, tool: AdkCcTool, args: dict, tool_context: ToolContext, *, persist: bool
    ) -> None:
        """Grant the target's parent directory (widens the sandbox scope) plus
        `<dir>/*` ALLOW rules for the destructive path tools, so subsequent
        writes there don't re-prompt. `persist` → the grant + rules go to `user:`
        scope (the persistent "Working directories" set)."""
        resolved = self._resolved_target(tool, args, tool_context)
        if not resolved:
            return
        parent = os.path.dirname(resolved)
        from ..sandbox import add_granted_root

        add_granted_root(tool_context, parent, persist=persist)

        key = _USER_ALLOW_STATE_KEY if persist else _SESSION_ALLOW_STATE_KEY
        existing = list(tool_context.state.get(key) or [])
        dir_glob = glob.escape(parent) + "/*"  # escape [ ] * ? in the dir path
        for tname in ("write_file", "edit_file"):
            rule = PermissionRule(
                source=RuleSource.SESSION,
                behavior=RuleBehavior.ALLOW,
                tool_name=tname,
                rule_content=dir_glob,
            )
            existing.append(rule.model_dump(mode="json"))
        tool_context.state[key] = existing
        emit_state_mutation(
            mutation_type="scope_granted",
            state_key="adk_cc_extra_roots",
            details={"directory": parent, "persist": persist},
            ctx=tool_context,
        )

    def _add_session_allow(
        self,
        tool: AdkCcTool | str,
        args: dict,
        tool_context: ToolContext,
        *,
        persist_across_sessions: bool = False,
        workspace_root: Optional[str] = None,
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
            `glob_files`) → workspace-anchored: when the target is
            inside `workspace_root` (the bound project in desktop mode),
            TWO rules — the literal path plus `<root>/*`, so one click
            covers the whole project. Out-of-workspace targets → ONE
            rule (literal), nothing safe to anchor to.
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
        tool_name = tool if isinstance(tool, str) else tool.meta.name
        contents = compute_allow_always_rule_contents(
            tool_name, args, workspace_root=workspace_root
        )

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

    def _workspace_root(self, tool_context: ToolContext) -> Optional[str]:
        """Canonical abs path of the session's workspace root, or None if it
        can't be resolved. In desktop in-place mode this is the bound project
        root; path-tool rules anchor to it. Lazy import keeps the plugin's
        module load independent of the sandbox package."""
        try:
            from ..sandbox import get_workspace

            return get_workspace(tool_context).abs_path
        except Exception:
            return None

    def _remote_home(self, tool_context: ToolContext) -> Optional[str]:
        """The REMOTE $HOME when this session's workspace lives on another
        machine (SshBackend), else None. Non-None flips `decide()` into
        remote path semantics: lexical resolution + the protected floor
        matched against the REMOTE home — without this the floor would guard
        the LOCAL ~/.ssh while the agent reads the remote's. The backend
        probes $HOME during ensure_workspace (tenancy runs before this
        plugin), so it's populated by the time any tool is decided."""
        try:
            from ..sandbox import get_backend, get_workspace

            if not getattr(get_workspace(tool_context), "remote", False):
                return None
            return getattr(get_backend(tool_context), "remote_home", None)
        except Exception:
            return None

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
