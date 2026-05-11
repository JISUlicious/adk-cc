"""Regression tests for the plan-mode tools' env-default fallback.

Bug (companion to PR #4's plugin-side fix): with
`ADK_CC_PERMISSION_MODE=plan` set in env, a fresh session is in plan-mode
posture via the plugin-layer fallback, but `state["permission_mode"]`
itself is unwritten. `ExitPlanModeTool._execute` reads
`state.get("permission_mode")` → None, the `previous != "plan"` guard
trips, the tool returns `noop`, and state never flips to `"default"`.
Next turn the plugins fall back to env-default=`"plan"` again — stuck
loop, no way out.

The fix: each plan-mode tool takes a `default_mode` ctor parameter
(wired to `PERMISSION_MODE.value` in agent.py) and uses it as a
fallback when state is unset.

Covers:
  - ExitPlanModeTool with state unset + default_mode=plan → flips state
    to "default" (the fix).
  - ExitPlanModeTool with state="plan" → flips to "default" (legacy).
  - ExitPlanModeTool with state="default" → noop (legacy).
  - ExitPlanModeTool with state unset + default_mode=default → noop.
  - EnterPlanModeTool symmetric: state unset + default_mode=plan →
    noop ("already in plan mode") instead of misreporting "ok, switched".
  - EnterPlanModeTool with state unset + default_mode=default → sets
    state to "plan".

Run: `.venv/bin/python tests/test_plan_mode_tools_env_default.py`
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.tools.enter_plan_mode import EnterPlanModeArgs, EnterPlanModeTool
from adk_cc.tools.exit_plan_mode import ExitPlanModeArgs, ExitPlanModeTool


# --- Fake ToolContext -----------------------------------------------


class _FakeState:
    def __init__(self, data: Optional[dict] = None) -> None:
        self._d: dict = dict(data or {})

    def get(self, k, default=None):
        return self._d.get(k, default)

    def __getitem__(self, k):
        return self._d[k]

    def __setitem__(self, k, v):
        self._d[k] = v

    def __contains__(self, k):
        return k in self._d


class _FakeToolContext:
    def __init__(self, state: Optional[dict] = None) -> None:
        self.state = _FakeState(state)


def _run_exit(tool: ExitPlanModeTool, ctx: _FakeToolContext, summary: str = "test plan") -> dict:
    args = ExitPlanModeArgs(plan_summary=summary)
    return asyncio.run(tool._execute(args, ctx))


def _run_enter(tool: EnterPlanModeTool, ctx: _FakeToolContext, reason: str = "test reason") -> dict:
    args = EnterPlanModeArgs(reason=reason)
    return asyncio.run(tool._execute(args, ctx))


# --- ExitPlanModeTool ----------------------------------------------


def test_exit_flips_state_when_explicit_plan() -> None:
    """state["permission_mode"]="plan" → tool flips to "default" (the
    legacy-correct path; was working before, should still work)."""
    tool = ExitPlanModeTool(default_mode="default")
    ctx = _FakeToolContext({"permission_mode": "plan"})
    out = _run_exit(tool, ctx)
    assert out["status"] == "approved", out
    assert out["previous_mode"] == "plan", out
    assert out["new_mode"] == "default", out
    assert ctx.state["permission_mode"] == "default", ctx.state._d
    print("OK test_exit_flips_state_when_explicit_plan")


def test_exit_flips_state_when_state_unset_but_env_plan() -> None:
    """THE FIX: state empty + env default=plan → tool now correctly sees
    "in plan mode" and flips state to "default". Without the fallback,
    this returned noop and state stayed unset."""
    tool = ExitPlanModeTool(default_mode="plan")
    ctx = _FakeToolContext()  # state empty
    out = _run_exit(tool, ctx)
    assert out["status"] == "approved", out
    assert out["previous_mode"] == "plan", out
    assert ctx.state["permission_mode"] == "default", ctx.state._d
    print("OK test_exit_flips_state_when_state_unset_but_env_plan")


def test_exit_noop_when_state_default() -> None:
    tool = ExitPlanModeTool(default_mode="default")
    ctx = _FakeToolContext({"permission_mode": "default"})
    out = _run_exit(tool, ctx)
    assert out["status"] == "noop", out
    # State unchanged.
    assert ctx.state["permission_mode"] == "default", ctx.state._d
    print("OK test_exit_noop_when_state_default")


def test_exit_noop_when_state_unset_and_env_default() -> None:
    """state empty + env default=default → tool sees "not in plan mode"
    via the fallback → returns noop."""
    tool = ExitPlanModeTool(default_mode="default")
    ctx = _FakeToolContext()
    out = _run_exit(tool, ctx)
    assert out["status"] == "noop", out
    assert out["current_mode"] == "default", out
    # No state was written.
    assert "permission_mode" not in ctx.state, ctx.state._d
    print("OK test_exit_noop_when_state_unset_and_env_default")


def test_exit_default_mode_ctor_default() -> None:
    """Constructor's default_mode default is "default" — preserves
    legacy behavior when agent.py doesn't pass anything."""
    tool = ExitPlanModeTool()
    assert tool._default_mode == "default"
    print("OK test_exit_default_mode_ctor_default")


# --- EnterPlanModeTool ---------------------------------------------


def test_enter_flips_state_when_state_unset_and_env_default() -> None:
    """state empty + env default=default → tool flips state to "plan"."""
    tool = EnterPlanModeTool(default_mode="default")
    ctx = _FakeToolContext()
    out = _run_enter(tool, ctx)
    assert out["status"] == "ok", out
    assert out["previous_mode"] == "default", out
    assert ctx.state["permission_mode"] == "plan", ctx.state._d
    print("OK test_enter_flips_state_when_state_unset_and_env_default")


def test_enter_noop_when_state_unset_but_env_plan() -> None:
    """THE FIX: state empty + env default=plan → tool now correctly
    identifies as "already in plan mode" via the fallback. Without it,
    the tool would have falsely reported "ok, switched to plan"."""
    tool = EnterPlanModeTool(default_mode="plan")
    ctx = _FakeToolContext()
    out = _run_enter(tool, ctx)
    assert out["status"] == "noop", out
    assert out["current_mode"] == "plan", out
    # State NOT written — already effectively in plan mode.
    assert "permission_mode" not in ctx.state, ctx.state._d
    print("OK test_enter_noop_when_state_unset_but_env_plan")


def test_enter_noop_when_state_explicit_plan() -> None:
    tool = EnterPlanModeTool(default_mode="default")
    ctx = _FakeToolContext({"permission_mode": "plan"})
    out = _run_enter(tool, ctx)
    assert out["status"] == "noop", out
    assert ctx.state["permission_mode"] == "plan", ctx.state._d
    print("OK test_enter_noop_when_state_explicit_plan")


def test_enter_flips_state_when_explicit_default() -> None:
    tool = EnterPlanModeTool(default_mode="default")
    ctx = _FakeToolContext({"permission_mode": "default"})
    out = _run_enter(tool, ctx)
    assert out["status"] == "ok", out
    assert out["previous_mode"] == "default", out
    assert ctx.state["permission_mode"] == "plan", ctx.state._d
    print("OK test_enter_flips_state_when_explicit_default")


def test_enter_default_mode_ctor_default() -> None:
    tool = EnterPlanModeTool()
    assert tool._default_mode == "default"
    print("OK test_enter_default_mode_ctor_default")


# --- End-to-end deadlock reproduction ------------------------------


def test_reproduces_and_resolves_env_plan_stuck_loop() -> None:
    """The reported scenario end-to-end:
      - Session boots with env=plan, state empty.
      - Model calls exit_plan_mode → state["permission_mode"]="default".
      - Subsequent reads of state see "default" (plugin's plan-mode
        filter would NOT fire — write tools surface again)."""
    tool = ExitPlanModeTool(default_mode="plan")
    ctx = _FakeToolContext()  # fresh session

    # Before exit: state has no permission_mode (env-default=plan is implicit).
    assert "permission_mode" not in ctx.state, ctx.state._d

    # User approves exit.
    out = _run_exit(tool, ctx, summary="my plan")
    assert out["status"] == "approved"

    # After: state explicitly says "default" — plugins reading
    # state["permission_mode"] see "default", which is truthy, so the
    # env-default fallback in PlanModeReminderPlugin does NOT kick in
    # (the fallback fires on `if not mode:`, and "default" is truthy).
    assert ctx.state["permission_mode"] == "default", ctx.state._d
    print("OK test_reproduces_and_resolves_env_plan_stuck_loop")


# --- Driver ---------------------------------------------------------


def main() -> None:
    test_exit_flips_state_when_explicit_plan()
    test_exit_flips_state_when_state_unset_but_env_plan()
    test_exit_noop_when_state_default()
    test_exit_noop_when_state_unset_and_env_default()
    test_exit_default_mode_ctor_default()
    test_enter_flips_state_when_state_unset_and_env_default()
    test_enter_noop_when_state_unset_but_env_plan()
    test_enter_noop_when_state_explicit_plan()
    test_enter_flips_state_when_explicit_default()
    test_enter_default_mode_ctor_default()
    test_reproduces_and_resolves_env_plan_stuck_loop()
    print("\nall plan-mode-tools env-default tests passed")


if __name__ == "__main__":
    main()
