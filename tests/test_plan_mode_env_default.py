"""Regression tests for the plan-mode env-default fallback.

Bug: when `ADK_CC_PERMISSION_MODE=plan` is set in env, `PermissionPlugin`
correctly defaults to PLAN mode (gating write/exec), but plugins that
filter the tool surface based on `state["permission_mode"]` see `None`
(state hasn't been written yet on a fresh session) and treat the
session as NORMAL — hiding `exit_plan_mode` and showing
`enter_plan_mode`. The user is then stuck: write tools blocked AND no
way to leave plan mode.

The fix: each plugin reading `state["permission_mode"]` accepts a
`default_mode` parameter and falls back to it when state is unset.

Three plugins involved:

  - PlanModeReminderPlugin (tool visibility filter) — primary symptom.
  - TaskReminderPlugin (skip reminder in plan mode) — minor symptom.
  - ToolCallValidatorPlugin (hint mentions plan mode) — minor symptom.

Run: `.venv/bin/python tests/test_plan_mode_env_default.py`
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from google.adk.models.llm_request import LlmRequest
from google.genai import types

from adk_cc.plugins.plan_mode import (
    _NORMAL_MODE_HIDDEN_TOOLS,
    _PLAN_MODE_HIDDEN_TOOLS,
    PlanModeReminderPlugin,
)
from adk_cc.plugins.task_reminder import TaskReminderPlugin
from adk_cc.plugins.tool_call_validator import ToolCallValidatorPlugin


# --- Fakes ----------------------------------------------------------


class _FakeState:
    def __init__(self, data: Optional[dict] = None) -> None:
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value


class _FakeCallbackContext:
    def __init__(
        self, *, state: Optional[dict] = None, agent_name: str = "coordinator"
    ) -> None:
        self.state = _FakeState(state)
        self.agent_name = agent_name
        # Provide a session-like object for TaskReminderPlugin.
        self.session = type("S", (), {"events": []})()


class _FakeToolContext:
    def __init__(self, *, state: Optional[dict] = None) -> None:
        self.state = _FakeState(state)


def _llm_request_with_tools(*names: str) -> LlmRequest:
    """Build a minimal LlmRequest whose tools_dict and config.tools list
    contain placeholder tool entries for `names`. The plugin filter
    mutates both surfaces; we read them back to check filtering."""
    # Each "tool" is just a sentinel object the plugin can pop/edit; we
    # don't actually call them. We do need `function_declarations` on
    # the config.tools side for the filter to find them.
    tools_dict = {name: object() for name in names}

    class _FakeToolObj:
        def __init__(self, decls):
            self.function_declarations = list(decls)

    decls = [types.FunctionDeclaration(name=n) for n in names]
    cfg = types.GenerateContentConfig(tools=[_FakeToolObj(decls)])
    req = LlmRequest(model="fake/m", config=cfg)
    req.tools_dict = tools_dict
    return req


# --- PlanModeReminderPlugin (primary bug) ---------------------------


def test_fresh_session_with_plan_env_default_shows_exit_plan_mode() -> None:
    """The bug: with env default = plan and state unset, exit_plan_mode
    used to be hidden. The fix: plugin falls back to default_mode."""
    plugin = PlanModeReminderPlugin(default_mode="plan")
    ctx = _FakeCallbackContext(state=None)  # fresh session, state empty
    req = _llm_request_with_tools(
        "read_file", "write_file", "run_bash", "edit_file",
        "enter_plan_mode", "exit_plan_mode",
        "task_create", "task_update", "ask_user_question",
    )

    asyncio.run(plugin.before_model_callback(callback_context=ctx, llm_request=req))

    surface = set(req.tools_dict.keys())
    # exit_plan_mode is visible (was the hidden one before the fix)
    assert "exit_plan_mode" in surface, surface
    # Plan-mode posture enforced even with empty state:
    for hidden in _PLAN_MODE_HIDDEN_TOOLS:
        assert hidden not in surface, (hidden, surface)
    # Read tools + plan tools + ask_user_question survive
    for kept in ("read_file", "exit_plan_mode", "ask_user_question"):
        assert kept in surface, (kept, surface)
    # Check both surfaces — both should be filtered consistently.
    decls = req.config.tools[0].function_declarations
    decl_names = {d.name for d in decls}
    assert "exit_plan_mode" in decl_names
    for hidden in _PLAN_MODE_HIDDEN_TOOLS:
        assert hidden not in decl_names, (hidden, decl_names)
    print("OK test_fresh_session_with_plan_env_default_shows_exit_plan_mode")


def test_state_overrides_env_default() -> None:
    """If state has been written (by enter_plan_mode/exit_plan_mode), it
    wins over the env default. Verify the default mode doesn't override
    runtime state when state is explicit."""
    plugin = PlanModeReminderPlugin(default_mode="plan")
    # State explicitly says default (e.g., after exit_plan_mode).
    ctx = _FakeCallbackContext(state={"permission_mode": "default"})
    req = _llm_request_with_tools(
        "write_file", "run_bash", "enter_plan_mode", "exit_plan_mode",
    )

    asyncio.run(plugin.before_model_callback(callback_context=ctx, llm_request=req))

    surface = set(req.tools_dict.keys())
    # State is "default", so write tools visible, exit_plan_mode hidden.
    assert "write_file" in surface
    assert "run_bash" in surface
    assert "enter_plan_mode" in surface
    assert "exit_plan_mode" not in surface, surface
    print("OK test_state_overrides_env_default")


def test_default_default_keeps_legacy_behavior() -> None:
    """When env default is "default" (the most common case) and state
    is unset, plugin treats as normal mode — the pre-fix behavior."""
    plugin = PlanModeReminderPlugin(default_mode="default")
    ctx = _FakeCallbackContext(state=None)
    req = _llm_request_with_tools("write_file", "exit_plan_mode")

    asyncio.run(plugin.before_model_callback(callback_context=ctx, llm_request=req))

    surface = set(req.tools_dict.keys())
    # Normal mode: write tools visible, exit_plan_mode hidden.
    assert "write_file" in surface
    for hidden in _NORMAL_MODE_HIDDEN_TOOLS:
        assert hidden not in surface, (hidden, surface)
    print("OK test_default_default_keeps_legacy_behavior")


def test_explicit_plan_state_works_without_env_default() -> None:
    """Without setting default_mode (defaults to "default") and state
    explicit plan, the existing flow keeps working."""
    plugin = PlanModeReminderPlugin()  # default_mode="default"
    ctx = _FakeCallbackContext(state={"permission_mode": "plan"})
    req = _llm_request_with_tools(
        "write_file", "exit_plan_mode", "enter_plan_mode", "read_file",
    )

    asyncio.run(plugin.before_model_callback(callback_context=ctx, llm_request=req))

    surface = set(req.tools_dict.keys())
    assert "exit_plan_mode" in surface, surface
    assert "write_file" not in surface, surface
    assert "enter_plan_mode" not in surface, surface
    assert "read_file" in surface
    print("OK test_explicit_plan_state_works_without_env_default")


def test_specialist_agents_unaffected() -> None:
    """Read-only specialists are skipped entirely; default_mode doesn't
    matter for them."""
    plugin = PlanModeReminderPlugin(default_mode="plan")
    ctx = _FakeCallbackContext(state=None, agent_name="Explore")
    req = _llm_request_with_tools("write_file", "exit_plan_mode")

    asyncio.run(plugin.before_model_callback(callback_context=ctx, llm_request=req))

    surface = set(req.tools_dict.keys())
    # Specialist short-circuits; nothing filtered (Explore wouldn't have
    # write_file anyway in real wiring — but the plugin doesn't touch).
    assert "write_file" in surface
    assert "exit_plan_mode" in surface
    print("OK test_specialist_agents_unaffected")


# --- TaskReminderPlugin (skip reminder in plan mode) ----------------


def test_task_reminder_respects_env_default_plan() -> None:
    """Reminder should NOT fire when env default is plan and state is
    unset (task tools are filtered out, no point reminding)."""
    plugin = TaskReminderPlugin(default_mode="plan")
    ctx = _FakeCallbackContext(state=None)
    req = _llm_request_with_tools("read_file")

    # Without the fix, this would proceed past the plan-mode gate and
    # try to read tasks (and potentially fire reminder). With the fix,
    # the gate returns None early.
    out = asyncio.run(plugin.before_model_callback(callback_context=ctx, llm_request=req))
    assert out is None
    # No system_instruction was added.
    assert req.config.system_instruction is None
    print("OK test_task_reminder_respects_env_default_plan")


def test_task_reminder_default_default_proceeds_normally() -> None:
    """When env default is "default" and state unset, the plan-mode gate
    is open — plugin proceeds to its actual reminder logic (which then
    may or may not fire based on turn counters)."""
    plugin = TaskReminderPlugin(default_mode="default")
    ctx = _FakeCallbackContext(state=None)
    req = _llm_request_with_tools("read_file")

    # No events / no task calls, so the turn-counter logic returns
    # before firing. Still, the plan-mode gate did NOT short-circuit
    # the function — we exercise that path by just confirming no crash.
    asyncio.run(plugin.before_model_callback(callback_context=ctx, llm_request=req))
    print("OK test_task_reminder_default_default_proceeds_normally")


# --- ToolCallValidatorPlugin (hint mentions plan mode) --------------


def test_validator_plan_hint_under_env_default() -> None:
    """With env default = plan and state unset, the validator's hint
    should mention plan mode — pre-fix it would not, because
    _in_plan_mode returned False."""
    plugin = ToolCallValidatorPlugin(default_mode="plan")
    ctx = _FakeToolContext(state=None)

    class _FakeTool:
        name = "write_file"

    error = ValueError(
        "Function write_file not found.\nAvailable tools: read_file, glob_files\n\n"
        "Possible causes: ..."
    )
    out = asyncio.run(
        plugin.on_tool_error_callback(
            tool=_FakeTool(), tool_args={"path": "/tmp/x"},
            tool_context=ctx, error=error,
        )
    )
    assert isinstance(out, dict), out
    hint = out["hint"]
    # The hint must reflect plan-mode context.
    assert "currently in plan mode" in hint, hint
    assert "exit_plan_mode" in hint, hint
    print("OK test_validator_plan_hint_under_env_default")


def test_validator_default_mode_omits_plan_hint() -> None:
    plugin = ToolCallValidatorPlugin(default_mode="default")
    ctx = _FakeToolContext(state=None)

    class _FakeTool:
        name = "write_file"

    error = ValueError(
        "Function write_file not found.\nAvailable tools: read_file\n\n"
        "Possible causes: ..."
    )
    out = asyncio.run(
        plugin.on_tool_error_callback(
            tool=_FakeTool(), tool_args={"path": "/tmp/x"},
            tool_context=ctx, error=error,
        )
    )
    assert isinstance(out, dict)
    hint = out["hint"]
    # Plan-mode hint should NOT appear in normal mode.
    assert "currently in plan mode" not in hint, hint
    assert "exit_plan_mode" not in hint, hint
    print("OK test_validator_default_mode_omits_plan_hint")


# --- Driver ---------------------------------------------------------


def main() -> None:
    test_fresh_session_with_plan_env_default_shows_exit_plan_mode()
    test_state_overrides_env_default()
    test_default_default_keeps_legacy_behavior()
    test_explicit_plan_state_works_without_env_default()
    test_specialist_agents_unaffected()
    test_task_reminder_respects_env_default_plan()
    test_task_reminder_default_default_proceeds_normally()
    test_validator_plan_hint_under_env_default()
    test_validator_default_mode_omits_plan_hint()
    print("\nall plan-mode env-default tests passed")


if __name__ == "__main__":
    main()
