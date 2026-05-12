"""Unit tests for the structured-payload confirmation UX in `PermissionPlugin`.

Covers:
  - Prompt helpers: `confirm_deny_prompt` (binary) and
    `allow_once_always_deny_prompt` (3-option, used by the destructive gate).
  - Call-site payload shape: the destructive gate sends a
    `single_select` structured `ConfirmPrompt` with 3 stable ids.
  - Resume paths:
      1. `chose_id="allow_once"` → run the tool, no session rule.
      2. `chose_id="allow_always"` → run the tool, SESSION ALLOW rule
         appended scoped to (tool, extracted rule key).
      3. `chose_id="allow"` (legacy two-option id) → run the tool,
         no session rule (back-compat).
      4. `chose_id="deny"` → permission_denied_by_user.
      5. legacy frontend, `confirmed=True`, no payload → run the tool.
      6. legacy frontend, `confirmed=False`, no payload → denied.
      7. malformed payload → falls back to `confirmed: bool`.
  - End-to-end: after `allow_always`, a second call with the same
    args is auto-allowed by the engine (the session rule did its job).

Run: `.venv/bin/python tests/test_permissions_confirmation.py`
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, ClassVar, Optional

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from pydantic import BaseModel

from adk_cc.permissions.confirmation import (
    ConfirmOption,
    ConfirmPrompt,
    _MAX_SUBJECT_LENGTH,
    allow_once_always_deny_prompt,
    confirm_deny_prompt,
    extract_subject,
)
from adk_cc.permissions.engine import decide
from adk_cc.permissions.modes import PermissionMode
from adk_cc.permissions.rules import RuleBehavior, RuleSource
from adk_cc.permissions.settings import SettingsHierarchy
from adk_cc.permissions.rules import PermissionRule
from adk_cc.plugins.permissions import (
    PermissionPlugin,
    _SESSION_ALLOW_STATE_KEY,
    _USER_ALLOW_STATE_KEY,
    _load_state_rules,
    _read_choice_id,
    _read_persist_toggle,
)
from adk_cc.tools.base import AdkCcTool, ToolMeta


# --- Fakes ----------------------------------------------------------


class _Args(BaseModel):
    command: str


class _FakeBashTool(AdkCcTool):
    """Stand-in AdkCcTool that decide() classifies as destructive.

    `is_destructive=True` triggers the destructive-tool fallback in
    the permission engine, so `decide()` returns `behavior="ask"` —
    exactly the branch we want to test.
    """

    meta: ClassVar[ToolMeta] = ToolMeta(
        name="run_bash",
        is_read_only=False,
        is_concurrency_safe=False,
        is_destructive=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = "fake bash"

    async def _execute(self, args: BaseModel, ctx: Any) -> dict:
        return {"status": "ran"}


class _FakeActions:
    def __init__(self) -> None:
        self.skip_summarization = False


class _FakeToolContext:
    """Minimal stand-in for ADK's ToolContext.

    Captures the args of `request_confirmation()` so tests can assert
    on the structured payload. Mirrors the attribute surface
    `PermissionPlugin.before_tool_callback` reads.
    """

    def __init__(
        self,
        *,
        tool_confirmation: Optional[Any] = None,
        state: Optional[dict] = None,
        function_call_id: str = "call-1",
    ) -> None:
        self.tool_confirmation = tool_confirmation
        self.state = state if state is not None else {}
        self.function_call_id = function_call_id
        self.actions = _FakeActions()
        self.requested: list[dict[str, Any]] = []

    def request_confirmation(
        self, *, hint: Optional[str] = None, payload: Optional[Any] = None
    ) -> None:
        self.requested.append({"hint": hint, "payload": payload})


class _FakeConfirmation:
    """Stand-in for ADK's ToolConfirmation."""

    def __init__(
        self,
        *,
        confirmed: bool = False,
        payload: Optional[Any] = None,
        hint: str = "",
    ) -> None:
        self.confirmed = confirmed
        self.payload = payload
        self.hint = hint


def _make_plugin(settings: Optional[SettingsHierarchy] = None) -> PermissionPlugin:
    # No explicit rules needed: `_FakeBashTool.meta.is_destructive=True`
    # makes `decide()` return "ask" via the destructive-tool fallback
    # in DEFAULT mode (engine.py step 4).
    return PermissionPlugin(
        settings if settings is not None else SettingsHierarchy(),
        default_mode=PermissionMode.DEFAULT,
    )


# --- Helper-level tests ---------------------------------------------


def test_confirm_deny_prompt_shape() -> None:
    """The two-button helper still produces the canonical binary prompt."""
    prompt = confirm_deny_prompt("run_bash", "destructive op")
    assert isinstance(prompt, ConfirmPrompt)
    assert prompt.style == "confirm_deny"
    assert prompt.title == "Confirm run_bash?"
    assert prompt.detail == "destructive op"
    assert [o.id for o in prompt.options] == ["allow", "deny"]
    for opt in prompt.options:
        assert isinstance(opt, ConfirmOption)
        assert opt.label and opt.description
    dumped = prompt.model_dump()
    assert dumped["style"] == "confirm_deny"
    print("OK test_confirm_deny_prompt_shape")


def test_confirm_deny_prompt_subject_in_title() -> None:
    """`subject` keyword goes into the title after a colon for disambig."""
    prompt = confirm_deny_prompt("run_bash", "x", subject="git status")
    assert prompt.title == "Confirm run_bash: git status?", prompt.title
    print("OK test_confirm_deny_prompt_subject_in_title")


def test_allow_once_always_deny_prompt_shape() -> None:
    """The 3-option helper produces a `single_select` prompt with the
    canonical ids in the expected order."""
    prompt = allow_once_always_deny_prompt("run_bash", "destructive op")
    assert isinstance(prompt, ConfirmPrompt)
    assert prompt.style == "single_select"
    assert prompt.title == "Confirm run_bash?"
    assert prompt.detail == "destructive op"
    assert [o.id for o in prompt.options] == ["allow_once", "allow_always", "deny"]
    for opt in prompt.options:
        assert opt.label and opt.description
    dumped = prompt.model_dump()
    assert dumped["style"] == "single_select"
    assert [o["id"] for o in dumped["options"]] == [
        "allow_once",
        "allow_always",
        "deny",
    ]
    print("OK test_allow_once_always_deny_prompt_shape")


def test_allow_once_always_deny_prompt_subject_in_title() -> None:
    """`subject` keyword goes into the title for the 3-option helper too."""
    prompt = allow_once_always_deny_prompt(
        "write_file", "destructive write_file requires confirmation",
        subject="/tmp/foo.txt",
    )
    assert prompt.title == "Confirm write_file: /tmp/foo.txt?", prompt.title
    # Options + detail unchanged by the subject.
    assert [o.id for o in prompt.options] == ["allow_once", "allow_always", "deny"]
    print("OK test_allow_once_always_deny_prompt_subject_in_title")


def test_extract_subject_per_tool() -> None:
    """`extract_subject` uses the engine's `_RULE_KEY_EXTRACTORS` to pick
    the right arg per tool: command for bash, path for file ops, etc."""
    assert extract_subject("run_bash", {"command": "git status"}) == "git status"
    assert extract_subject("write_file", {"path": "/tmp/foo.txt", "content": "..."}) == "/tmp/foo.txt"
    assert extract_subject("read_file", {"path": "/etc/hosts"}) == "/etc/hosts"
    assert extract_subject("edit_file", {"path": "/x", "old_string": "a", "new_string": "b"}) == "/x"
    assert extract_subject("glob_files", {"root": "src", "pattern": "**/*.py"}) == "src"
    assert extract_subject("grep", {"path": ".", "pattern": "TODO"}) == "."
    print("OK test_extract_subject_per_tool")


def test_extract_subject_truncates_long_strings() -> None:
    """A multi-hundred-char command gets clipped with an ellipsis so the
    title stays readable. Internal newlines collapse to spaces too."""
    cmd = "echo " + "x" * 500
    out = extract_subject("run_bash", {"command": cmd})
    assert out is not None
    assert len(out) == _MAX_SUBJECT_LENGTH, len(out)
    assert out.endswith("…")
    # Multi-line collapses to single line.
    multiline = extract_subject("run_bash", {"command": "git\nstatus\n-v"})
    assert multiline == "git status -v"
    print("OK test_extract_subject_truncates_long_strings")


def test_extract_subject_returns_none_for_unknown_tool() -> None:
    """Tools without a rule-key extractor entry (custom user tools)
    return None; the prompt title falls back to `Confirm <tool>?`."""
    assert extract_subject("some_custom_tool", {"path": "/x"}) is None
    assert extract_subject("ask_user_question", {}) is None
    # Known tool but empty arg.
    assert extract_subject("run_bash", {}) is None
    assert extract_subject("run_bash", {"command": ""}) is None
    print("OK test_extract_subject_returns_none_for_unknown_tool")


def test_read_choice_id_helper() -> None:
    """The helper tolerates every kind of garbage."""
    assert _read_choice_id(_FakeConfirmation(payload={"chose_id": "allow_once"})) == "allow_once"
    assert _read_choice_id(_FakeConfirmation(payload={"chose_id": "deny"})) == "deny"
    assert _read_choice_id(_FakeConfirmation(payload=None)) is None
    assert _read_choice_id(_FakeConfirmation(payload={})) is None
    assert _read_choice_id(_FakeConfirmation(payload="not-a-dict")) is None
    assert _read_choice_id(_FakeConfirmation(payload={"chose_id": 123})) is None
    print("OK test_read_choice_id_helper")


# --- before_tool_callback paths ------------------------------------


def test_first_call_emits_single_select_payload() -> None:
    """First invocation: plugin calls `request_confirmation` with a
    structured `single_select` payload AND sets skip_summarization."""
    plugin = _make_plugin()
    tool = _FakeBashTool()
    ctx = _FakeToolContext()

    result = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "rm -rf /tmp/foo"}, tool_context=ctx
        )
    )

    assert isinstance(result, dict) and result["status"] == "needs_confirmation", result
    assert len(ctx.requested) == 1, ctx.requested
    call = ctx.requested[0]
    assert call["hint"]
    payload = call["payload"]
    assert isinstance(payload, dict), payload
    assert payload["style"] == "single_select"
    # Subject (the command) is now in the title so concurrent prompts
    # for the same tool can be told apart.
    assert payload["title"] == "Confirm run_bash: rm -rf /tmp/foo?", payload["title"]
    assert payload["detail"] == call["hint"]
    assert [o["id"] for o in payload["options"]] == [
        "allow_once",
        "allow_always",
        "deny",
    ]
    assert ctx.actions.skip_summarization is True
    print("OK test_first_call_emits_single_select_payload")


def test_resume_allow_once_runs_tool_no_session_rule() -> None:
    """`chose_id=allow_once` lets the tool run and adds NO session rule."""
    settings = SettingsHierarchy()
    plugin = _make_plugin(settings)
    tool = _FakeBashTool()
    ctx = _FakeToolContext(
        tool_confirmation=_FakeConfirmation(payload={"chose_id": "allow_once"})
    )

    result = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "ls"}, tool_context=ctx
        )
    )
    assert result is None, result
    # No session rule was injected.
    assert settings.all_rules() == [], settings.all_rules()
    print("OK test_resume_allow_once_runs_tool_no_session_rule")


def test_resume_legacy_allow_id_back_compat() -> None:
    """`chose_id=allow` (the legacy two-option id) keeps working —
    treated as allow_once. No session rule appended."""
    settings = SettingsHierarchy()
    plugin = _make_plugin(settings)
    tool = _FakeBashTool()
    ctx = _FakeToolContext(
        tool_confirmation=_FakeConfirmation(payload={"chose_id": "allow"})
    )

    result = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "ls"}, tool_context=ctx
        )
    )
    assert result is None, result
    assert settings.all_rules() == [], settings.all_rules()
    print("OK test_resume_legacy_allow_id_back_compat")


def test_resume_allow_always_injects_session_rule() -> None:
    """`chose_id=allow_always` runs the tool AND appends a SESSION ALLOW
    rule scoped to (tool_name, extracted rule key). The rule lands in
    `state["adk_cc_allow_rules"]` (session-scope) — NOT in the
    in-memory `SettingsHierarchy.SESSION` layer, which is intentionally
    no longer mutated at runtime so we can store via session DB and
    survive agent restarts."""
    settings = SettingsHierarchy()
    plugin = _make_plugin(settings)
    tool = _FakeBashTool()
    ctx = _FakeToolContext(
        tool_confirmation=_FakeConfirmation(payload={"chose_id": "allow_always"})
    )

    result = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "git status"}, tool_context=ctx
        )
    )
    # Tool runs.
    assert result is None, result
    # In-memory hierarchy is unchanged — runtime rules live in state now.
    assert settings.all_rules() == []
    # Per-session list has exactly one rule with the right scope.
    raw = ctx.state[_SESSION_ALLOW_STATE_KEY]
    assert isinstance(raw, list) and len(raw) == 1, raw
    r = PermissionRule.model_validate(raw[0])
    assert r.source is RuleSource.SESSION
    assert r.behavior is RuleBehavior.ALLOW
    assert r.tool_name == "run_bash"
    assert r.rule_content == "git status"
    # No `user:`-prefixed entry — default is per-session only.
    assert _USER_ALLOW_STATE_KEY not in ctx.state, ctx.state
    print("OK test_resume_allow_always_injects_session_rule")


def test_resume_allow_always_with_persist_toggle_writes_user_state() -> None:
    """When the operator ticks the `persist_across_sessions` toggle,
    the resulting rule is written to the `user:`-prefixed state key
    so ADK persists it across the same user's future sessions."""
    settings = SettingsHierarchy()
    plugin = _make_plugin(settings)
    tool = _FakeBashTool()
    ctx = _FakeToolContext(
        tool_confirmation=_FakeConfirmation(
            payload={"chose_id": "allow_always", "persist_across_sessions": True}
        )
    )

    result = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "uv run pytest"}, tool_context=ctx
        )
    )
    assert result is None, result
    # Rule landed in the user-scope key, NOT the session-scope key.
    assert _SESSION_ALLOW_STATE_KEY not in ctx.state, ctx.state
    raw = ctx.state[_USER_ALLOW_STATE_KEY]
    assert isinstance(raw, list) and len(raw) == 1, raw
    r = PermissionRule.model_validate(raw[0])
    assert r.tool_name == "run_bash"
    assert r.rule_content == "uv run pytest"
    print("OK test_resume_allow_always_with_persist_toggle_writes_user_state")


def test_allow_always_skips_re_ask_on_second_call() -> None:
    """End-to-end: after allow_always, the SAME (tool, command) is
    auto-allowed by the engine on the next call — no second prompt.

    The state-backed rule survives because both calls share the same
    `ToolContext.state` (the session record). A different command in
    the same session still gates."""
    settings = SettingsHierarchy()
    plugin = _make_plugin(settings)
    tool = _FakeBashTool()

    # Shared state simulates a single session across two tool calls.
    shared_state: dict = {}

    # First call: user picks allow_always → rule lands in state.
    ctx1 = _FakeToolContext(
        tool_confirmation=_FakeConfirmation(payload={"chose_id": "allow_always"}),
        state=shared_state,
    )
    asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "git status"}, tool_context=ctx1
        )
    )
    assert _SESSION_ALLOW_STATE_KEY in shared_state

    # Second call, same command: plugin's `_effective_settings` merges
    # in the state-backed rule, decide returns `allow`, no re-ask.
    ctx2 = _FakeToolContext(state=shared_state)
    result2 = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "git status"}, tool_context=ctx2
        )
    )
    assert result2 is None, result2  # allowed → no override returned

    # Different command (not covered by the rule): still gates.
    ctx3 = _FakeToolContext(state=shared_state)
    result3 = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "rm -rf /"}, tool_context=ctx3
        )
    )
    assert isinstance(result3, dict), result3
    assert result3["status"] == "needs_confirmation", result3
    print("OK test_allow_always_skips_re_ask_on_second_call")


def test_allow_always_user_scope_survives_across_sessions() -> None:
    """Symmetric to the per-session test but for the `user:` prefix:
    after `allow_always` with persist=True in session A, a fresh
    session B (different state dict, but same user — same `user:`
    bucket) is auto-allowed.

    We simulate the `user:` persistence by carrying just that key
    forward to the second session. The session-scoped key is dropped
    (a new session starts empty), but the user-scoped key persists
    via ADK's State backend."""
    settings = SettingsHierarchy()
    plugin = _make_plugin(settings)
    tool = _FakeBashTool()

    state_a: dict = {}
    ctx_a = _FakeToolContext(
        tool_confirmation=_FakeConfirmation(
            payload={"chose_id": "allow_always", "persist_across_sessions": True}
        ),
        state=state_a,
    )
    asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "uv run pytest"}, tool_context=ctx_a
        )
    )
    # Persisted under user:, not session:.
    assert _USER_ALLOW_STATE_KEY in state_a

    # New session — carry only the `user:` bucket forward.
    state_b: dict = {_USER_ALLOW_STATE_KEY: state_a[_USER_ALLOW_STATE_KEY]}
    ctx_b = _FakeToolContext(state=state_b)
    result_b = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "uv run pytest"}, tool_context=ctx_b
        )
    )
    assert result_b is None, result_b  # auto-allowed — no prompt.
    print("OK test_allow_always_user_scope_survives_across_sessions")


def test_resume_deny_via_payload() -> None:
    """`chose_id=deny` short-circuits."""
    plugin = _make_plugin()
    tool = _FakeBashTool()
    ctx = _FakeToolContext(
        tool_confirmation=_FakeConfirmation(payload={"chose_id": "deny"})
    )

    result = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "ls"}, tool_context=ctx
        )
    )
    assert isinstance(result, dict), result
    assert result["status"] == "permission_denied_by_user", result
    print("OK test_resume_deny_via_payload")


def test_resume_legacy_confirmed_true() -> None:
    """No payload + confirmed=True → run the tool (legacy `adk web` path)."""
    plugin = _make_plugin()
    tool = _FakeBashTool()
    ctx = _FakeToolContext(
        tool_confirmation=_FakeConfirmation(confirmed=True, payload=None)
    )

    result = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "ls"}, tool_context=ctx
        )
    )
    assert result is None, result
    print("OK test_resume_legacy_confirmed_true")


def test_resume_legacy_confirmed_false() -> None:
    """No payload + confirmed=False → denied (legacy path)."""
    plugin = _make_plugin()
    tool = _FakeBashTool()
    ctx = _FakeToolContext(
        tool_confirmation=_FakeConfirmation(confirmed=False, payload=None)
    )

    result = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "ls"}, tool_context=ctx
        )
    )
    assert isinstance(result, dict), result
    assert result["status"] == "permission_denied_by_user", result
    print("OK test_resume_legacy_confirmed_false")


def test_read_persist_toggle_helper() -> None:
    """True only when `payload['persist_across_sessions'] is True`;
    everything else (missing, truthy non-bool, wrong shape) → False."""
    # Confirmed-with-toggle.
    yes = _FakeConfirmation(payload={"chose_id": "allow_always", "persist_across_sessions": True})
    assert _read_persist_toggle(yes) is True
    # Explicit False.
    no = _FakeConfirmation(payload={"chose_id": "allow_always", "persist_across_sessions": False})
    assert _read_persist_toggle(no) is False
    # Missing.
    missing = _FakeConfirmation(payload={"chose_id": "allow_always"})
    assert _read_persist_toggle(missing) is False
    # Truthy non-bool (string "true") — must NOT count; we require strict True.
    truthy = _FakeConfirmation(payload={"persist_across_sessions": "true"})
    assert _read_persist_toggle(truthy) is False
    # Garbage payload shapes don't crash.
    assert _read_persist_toggle(_FakeConfirmation(payload=None)) is False
    assert _read_persist_toggle(_FakeConfirmation(payload="not a dict")) is False
    print("OK test_read_persist_toggle_helper")


def test_load_state_rules_helper() -> None:
    """Pulls + deserializes rules from BOTH state keys, in (session,
    user) order. Skips malformed entries silently."""
    rule_s = PermissionRule(
        source=RuleSource.SESSION, behavior=RuleBehavior.ALLOW,
        tool_name="run_bash", rule_content="git status",
    ).model_dump(mode="json")
    rule_u = PermissionRule(
        source=RuleSource.SESSION, behavior=RuleBehavior.ALLOW,
        tool_name="run_bash", rule_content="uv run pytest",
    ).model_dump(mode="json")

    ctx = _FakeToolContext(state={
        _SESSION_ALLOW_STATE_KEY: [rule_s, "not a dict", {"missing": "fields"}],
        _USER_ALLOW_STATE_KEY: [rule_u],
    })
    out = _load_state_rules(ctx)
    # Two valid rules; the two malformed entries are silently skipped.
    assert len(out) == 2, out
    assert out[0].rule_content == "git status"  # session first
    assert out[1].rule_content == "uv run pytest"  # then user

    # Missing keys → empty.
    empty_ctx = _FakeToolContext(state={})
    assert _load_state_rules(empty_ctx) == []

    # Non-list values → ignored, not crashed.
    bad_ctx = _FakeToolContext(state={_SESSION_ALLOW_STATE_KEY: "not a list"})
    assert _load_state_rules(bad_ctx) == []
    print("OK test_load_state_rules_helper")


def test_resume_malformed_payload_falls_back() -> None:
    """Garbage `chose_id` doesn't crash.

    A bogus string id (e.g. "bogus") falls through to the denied branch
    because it's not one of the known ids. A non-string id (e.g. integer)
    collapses to None in `_read_choice_id`, which then consults `confirmed`.
    """
    plugin = _make_plugin()
    tool = _FakeBashTool()

    # Bogus string id, confirmed=True → still denied. Unknown ids fail closed.
    ctx_a = _FakeToolContext(
        tool_confirmation=_FakeConfirmation(
            confirmed=True, payload={"chose_id": "bogus"}
        )
    )
    result_a = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "ls"}, tool_context=ctx_a
        )
    )
    assert isinstance(result_a, dict), result_a
    assert result_a["status"] == "permission_denied_by_user", result_a

    # Non-string chose_id → helper returns None, falls back to `confirmed`.
    ctx_b = _FakeToolContext(
        tool_confirmation=_FakeConfirmation(
            confirmed=True, payload={"chose_id": 42}
        )
    )
    result_b = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "ls"}, tool_context=ctx_b
        )
    )
    assert result_b is None, result_b
    print("OK test_resume_malformed_payload_falls_back")


def main() -> None:
    test_confirm_deny_prompt_shape()
    test_confirm_deny_prompt_subject_in_title()
    test_allow_once_always_deny_prompt_shape()
    test_allow_once_always_deny_prompt_subject_in_title()
    test_extract_subject_per_tool()
    test_extract_subject_truncates_long_strings()
    test_extract_subject_returns_none_for_unknown_tool()
    test_read_choice_id_helper()
    test_first_call_emits_single_select_payload()
    test_resume_allow_once_runs_tool_no_session_rule()
    test_resume_legacy_allow_id_back_compat()
    test_resume_allow_always_injects_session_rule()
    test_resume_allow_always_with_persist_toggle_writes_user_state()
    test_allow_always_skips_re_ask_on_second_call()
    test_allow_always_user_scope_survives_across_sessions()
    test_read_persist_toggle_helper()
    test_load_state_rules_helper()
    test_resume_deny_via_payload()
    test_resume_legacy_confirmed_true()
    test_resume_legacy_confirmed_false()
    test_resume_malformed_payload_falls_back()
    print("\nall permissions-confirmation tests passed")


if __name__ == "__main__":
    main()
