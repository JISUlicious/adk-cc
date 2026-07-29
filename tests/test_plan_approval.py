"""Plan approval: the third option, and approving straight from `write_plan`.

Two behaviours worth pinning because both are easy to get subtly wrong:

1. **Revise is not approval.** The confirmation layer computes
   `confirmed = chose_id != "deny"`, so "revise" arrives at the tool looking
   exactly like an approval. If a tool forgets to intercept it, asking for a
   small change silently exits plan mode and starts the implementation.
2. **Approving from `write_plan` exits plan mode.** Otherwise the second round
   trip this option removes comes straight back.

Run: uv run python tests/test_plan_approval.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.permissions.plan_approval import APPROVE, DENY, REVISE  # noqa: E402
from adk_cc.tools.exit_plan_mode import ExitPlanModeArgs, ExitPlanModeTool  # noqa: E402
from adk_cc.tools.schemas import WritePlanArgs  # noqa: E402
from adk_cc.tools.write_plan import WritePlanTool  # noqa: E402

_WS = tempfile.mkdtemp(prefix="planapp-")


class _Confirmation:
    def __init__(self, chose, comment=None, confirmed=True):
        self.confirmed = confirmed
        self.payload = {"chose_id": chose}
        if comment:
            self.payload["comment"] = comment


class _Ctx:
    agent_name = "coordinator"

    def __init__(self, chose=None, comment=None, mode="plan"):
        self.state = {"permission_mode": mode, "plan_previous_mode": "default"}
        self.tool_confirmation = _Confirmation(chose, comment) if chose else None
        self.actions = type("A", (), {"skip_summarization": False})()
        self.function_call_id = "fc-1"


class _Backend:
    def __init__(self):
        self.written = {}

    async def write_text(self, path, content, fs_write=None):
        self.written[path] = content


class _Ws:
    abs_path = _WS

    def fs_write_config(self):
        return None


def _patch_ws():
    import adk_cc.tools.write_plan as wp

    backend = _Backend()
    wp.get_backend = lambda ctx: backend
    wp.get_workspace = lambda ctx: _Ws()
    return backend


def test_all_three_options_are_offered() -> None:
    for payload in (
        ExitPlanModeTool()._approval_payload(ExitPlanModeArgs(plan_summary="s")),
        WritePlanTool()._approval_payload(
            WritePlanArgs(content="# Plan\nstep", request_approval=True)),
    ):
        ids = [o["id"] for o in payload["options"]]
        assert ids == [APPROVE, REVISE, DENY], ids
        assert payload["with_comment"] is True
    print("OK all_three_options_are_offered")


def test_revise_does_not_exit_plan_mode() -> None:
    tool, ctx = ExitPlanModeTool(), _Ctx(chose=REVISE, comment="add rollback")
    res = asyncio.run(tool._execute(ExitPlanModeArgs(plan_summary="ship it"), ctx))
    assert res["status"] == "revision_requested", res
    assert res["user_comment"] == "add rollback", res
    assert ctx.state["permission_mode"] == "plan", "revise must NOT exit plan mode"
    # the marker is still there for the eventual real approval
    assert ctx.state["plan_previous_mode"] == "default", ctx.state
    print("OK revise_does_not_exit_plan_mode")


def test_write_plan_drafts_without_asking() -> None:
    backend = _patch_ws()
    tool = WritePlanTool()
    draft = WritePlanArgs(content="# Draft\nwip")
    assert tool._requires_approval(draft) is False
    ctx = _Ctx()
    res = asyncio.run(tool._execute(draft, ctx))
    assert res["status"] == "ok", res
    assert ctx.state["permission_mode"] == "plan", "a draft must not exit plan mode"
    assert backend.written, "the draft was not written"
    print("OK write_plan_drafts_without_asking")


def test_write_plan_approval_writes_and_exits() -> None:
    backend = _patch_ws()
    tool = WritePlanTool()
    ready = WritePlanArgs(content="# Plan\ndo the thing", request_approval=True)
    assert tool._requires_approval(ready) is True
    ctx = _Ctx(chose=APPROVE)
    res = asyncio.run(tool._execute(ready, ctx))
    assert res["status"] == "approved", res
    assert res["new_mode"] == "default" and res["previous_mode"] == "plan", res
    assert res["path"].endswith(".md") and backend.written, res
    assert ctx.state["permission_mode"] == "default", "approval must exit plan mode"
    assert ctx.state["plan_previous_mode"] is None, "marker must be consumed"
    print("OK write_plan_approval_writes_and_exits")


def test_write_plan_revise_writes_nothing() -> None:
    backend = _patch_ws()
    tool = WritePlanTool()
    ready = WritePlanArgs(content="# Plan\nv1", request_approval=True)
    ctx = _Ctx(chose=REVISE, comment="smaller scope")
    res = asyncio.run(tool._execute(ready, ctx))
    assert res["status"] == "revision_requested", res
    assert res["user_comment"] == "smaller scope", res
    assert ctx.state["permission_mode"] == "plan", "revise must stay in plan mode"
    assert not backend.written, "a plan awaiting revision should not be filed"
    print("OK write_plan_revise_writes_nothing")


def test_approval_outside_plan_mode_is_a_noop_not_a_lie() -> None:
    _patch_ws()
    ctx = _Ctx(chose=APPROVE, mode="default")
    res = asyncio.run(WritePlanTool()._execute(
        WritePlanArgs(content="# Plan\nx", request_approval=True), ctx))
    # the plan is still written; the mode change correctly reports noop
    assert res["path"].endswith(".md"), res
    assert res["status"] == "noop" or res.get("current_mode") == "default", res
    print("OK approval_outside_plan_mode_is_a_noop_not_a_lie")


def main() -> None:
    test_all_three_options_are_offered()
    test_revise_does_not_exit_plan_mode()
    test_write_plan_drafts_without_asking()
    test_write_plan_approval_writes_and_exits()
    test_write_plan_revise_writes_nothing()
    test_approval_outside_plan_mode_is_a_noop_not_a_lie()
    print("\nall plan-approval tests passed")


if __name__ == "__main__":
    main()
