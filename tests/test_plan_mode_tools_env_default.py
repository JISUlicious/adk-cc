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

from adk_cc.tools.base import _extract_user_comment
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


class _FakeActions:
    """Minimal stand-in for ADK's EventActions — only the field
    AdkCcTool.run_async assigns to."""

    def __init__(self) -> None:
        self.skip_summarization: bool = False


class _FakeConfirmation:
    """Minimal stand-in for ADK's ToolConfirmation. The base-tool deny
    branch only reads `.confirmed` and `.payload`."""

    def __init__(self, confirmed: bool, payload: Optional[dict] = None) -> None:
        self.confirmed = confirmed
        self.payload = payload


class _FakeToolContext:
    def __init__(
        self,
        state: Optional[dict] = None,
        *,
        tool_confirmation: Optional[_FakeConfirmation] = None,
    ) -> None:
        self.state = _FakeState(state)
        self.actions = _FakeActions()
        self.tool_confirmation = tool_confirmation

    def request_confirmation(self, *, hint=None, payload=None) -> None:  # noqa: ARG002
        # First-invocation path isn't exercised in these tests; provide a
        # no-op so a stray call doesn't AttributeError.
        return None


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


# --- exit_plan_mode deny / approve-with-comment --------------------
#
# These cover the ConfirmPrompt-shaped `_approval_payload`, the deny
# branch in AdkCcTool.run_async, and the approve path's user_comment
# pass-through in ExitPlanModeTool._execute. See PR-in-progress
# feat/confirmation-deny-with-comment.


def test_extract_user_comment_helper() -> None:
    """The shared base-tool helper strips/normalises payload['comment']
    and returns None on absent/empty/non-string."""
    # Absent / wrong shape.
    assert _extract_user_comment(None) is None
    assert _extract_user_comment(_FakeConfirmation(False, None)) is None
    assert _extract_user_comment(_FakeConfirmation(False, {})) is None
    # Non-string.
    assert _extract_user_comment(_FakeConfirmation(False, {"comment": 42})) is None
    # Empty / whitespace-only → None (don't surface a useless field).
    assert _extract_user_comment(_FakeConfirmation(False, {"comment": ""})) is None
    assert _extract_user_comment(_FakeConfirmation(False, {"comment": "   "})) is None
    # Real text gets stripped.
    out = _extract_user_comment(_FakeConfirmation(False, {"comment": "  try smaller scope  "}))
    assert out == "try smaller scope"
    print("OK test_extract_user_comment_helper")


def test_approval_payload_has_approve_deny_with_comment() -> None:
    """`_approval_payload` returns a ConfirmPrompt-shaped dict with the
    two approve/deny options + with_comment=True so the bundled form-UI
    plugin renders a textbox. Also carries `plan_summary` for any
    frontend that wants to render it separately from `detail`."""
    tool = ExitPlanModeTool(default_mode="plan")
    payload = tool._approval_payload(ExitPlanModeArgs(plan_summary="rewrite the auth middleware"))
    assert payload["style"] == "single_select", payload
    assert payload["title"] == "Exit plan mode?", payload
    assert payload["detail"] == "rewrite the auth middleware", payload
    assert payload["with_comment"] is True, payload
    ids = [opt["id"] for opt in payload["options"]]
    assert ids == ["approve", "deny"], ids
    # Plan summary is also surfaced verbatim for frontends that want
    # to render the plan body distinct from `detail`.
    assert payload["plan_summary"] == "rewrite the auth middleware", payload
    print("OK test_approval_payload_has_approve_deny_with_comment")


def test_run_async_denied_surfaces_user_comment() -> None:
    """When the operator denies the prompt and typed a comment into the
    form's textbox, AdkCcTool.run_async returns the comment on the
    denied response so the model can revise the plan."""
    tool = ExitPlanModeTool(default_mode="plan")
    confirmation = _FakeConfirmation(
        confirmed=False,
        payload={"chose_id": "deny", "comment": "too aggressive — split the refactor"},
    )
    ctx = _FakeToolContext({"permission_mode": "plan"}, tool_confirmation=confirmation)
    out = asyncio.run(
        tool.run_async(
            args={"plan_summary": "ship the whole rewrite at once"},
            tool_context=ctx,
        )
    )
    assert out["status"] == "denied", out
    assert out["user_comment"] == "too aggressive — split the refactor", out
    # State must NOT have flipped — denial means stay in plan mode.
    assert ctx.state["permission_mode"] == "plan", ctx.state._d
    print("OK test_run_async_denied_surfaces_user_comment")


def test_run_async_denied_without_comment_omits_field() -> None:
    """Empty / missing comment → response has no `user_comment` key
    (cleaner for the model than a noisy empty-string field)."""
    tool = ExitPlanModeTool(default_mode="plan")
    confirmation = _FakeConfirmation(
        confirmed=False, payload={"chose_id": "deny"}
    )
    ctx = _FakeToolContext({"permission_mode": "plan"}, tool_confirmation=confirmation)
    out = asyncio.run(
        tool.run_async(
            args={"plan_summary": "any plan"}, tool_context=ctx
        )
    )
    assert out["status"] == "denied", out
    assert "user_comment" not in out, out
    print("OK test_run_async_denied_without_comment_omits_field")


def test_run_async_approved_surfaces_user_comment() -> None:
    """Conditional approvals ("go ahead but be careful about X") — the
    operator approves AND types a comment. The model needs the comment
    on the approved response too, not only the denied one."""
    tool = ExitPlanModeTool(default_mode="plan")
    confirmation = _FakeConfirmation(
        confirmed=True,
        payload={"chose_id": "approve", "comment": "be careful with the DB migration"},
    )
    ctx = _FakeToolContext({"permission_mode": "plan"}, tool_confirmation=confirmation)
    out = asyncio.run(
        tool.run_async(
            args={"plan_summary": "rewrite auth middleware"}, tool_context=ctx
        )
    )
    assert out["status"] == "approved", out
    assert out["user_comment"] == "be careful with the DB migration", out
    # State flipped to default — approval took effect.
    assert ctx.state["permission_mode"] == "default", ctx.state._d
    print("OK test_run_async_approved_surfaces_user_comment")


def test_run_async_approved_without_comment_omits_field() -> None:
    tool = ExitPlanModeTool(default_mode="plan")
    confirmation = _FakeConfirmation(confirmed=True, payload={"chose_id": "approve"})
    ctx = _FakeToolContext({"permission_mode": "plan"}, tool_confirmation=confirmation)
    out = asyncio.run(
        tool.run_async(
            args={"plan_summary": "rewrite auth middleware"}, tool_context=ctx
        )
    )
    assert out["status"] == "approved", out
    assert "user_comment" not in out, out
    print("OK test_run_async_approved_without_comment_omits_field")


# --- Driver ---------------------------------------------------------


# --- F4: pre-plan mode restore (dogfooding fix) ---------------------


def test_enter_records_previous_mode() -> None:
    """enter_plan_mode persists the pre-plan mode for exit to restore."""
    tool = EnterPlanModeTool(default_mode="default")
    ctx = _FakeToolContext({"permission_mode": "bypasspermissions"})
    out = _run_enter(tool, ctx)
    assert out["status"] == "ok", out
    assert ctx.state["permission_mode"] == "plan", ctx.state._d
    assert ctx.state["plan_previous_mode"] == "bypasspermissions", ctx.state._d
    print("OK test_enter_records_previous_mode")


def test_exit_restores_recorded_previous_mode() -> None:
    """THE F4 FIX: bypassPermissions → plan → approve → bypassPermissions
    (was: hardcoded "default", turning every post-approval write into a
    confirmation prompt on desktop)."""
    ctx = _FakeToolContext({"permission_mode": "bypasspermissions"})
    _run_enter(EnterPlanModeTool(default_mode="default"), ctx)
    out = _run_exit(ExitPlanModeTool(default_mode="default"), ctx)
    assert out["status"] == "approved", out
    assert out["new_mode"] == "bypasspermissions", out
    assert ctx.state["permission_mode"] == "bypasspermissions", ctx.state._d
    # marker consumed — a later UI-toggled plan cycle must not inherit it
    assert not ctx.state.get("plan_previous_mode"), ctx.state._d
    print("OK test_exit_restores_recorded_previous_mode")


def test_exit_without_marker_falls_back_to_default() -> None:
    """Plan mode entered WITHOUT the tool (UI toggle patches state directly)
    → no marker → exit restores "default" (pre-fix behavior preserved)."""
    ctx = _FakeToolContext({"permission_mode": "plan"})
    out = _run_exit(ExitPlanModeTool(default_mode="default"), ctx)
    assert out["status"] == "approved", out
    assert out["new_mode"] == "default", out
    assert ctx.state["permission_mode"] == "default", ctx.state._d
    print("OK test_exit_without_marker_falls_back_to_default")


def test_exit_never_restores_into_plan() -> None:
    """A stale/corrupt marker saying "plan" must not trap the session."""
    ctx = _FakeToolContext(
        {"permission_mode": "plan", "plan_previous_mode": "plan"}
    )
    out = _run_exit(ExitPlanModeTool(default_mode="default"), ctx)
    assert out["status"] == "approved", out
    assert ctx.state["permission_mode"] == "default", ctx.state._d
    print("OK test_exit_never_restores_into_plan")


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
    test_extract_user_comment_helper()
    test_approval_payload_has_approve_deny_with_comment()
    test_run_async_denied_surfaces_user_comment()
    test_run_async_denied_without_comment_omits_field()
    test_run_async_approved_surfaces_user_comment()
    test_run_async_approved_without_comment_omits_field()
    test_enter_records_previous_mode()
    test_exit_restores_recorded_previous_mode()
    test_exit_without_marker_falls_back_to_default()
    test_exit_never_restores_into_plan()
    print("\nall plan-mode-tools env-default tests passed")


if __name__ == "__main__":
    main()
