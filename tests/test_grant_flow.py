"""Integration tests for the desktop scope-expansion grant flow.

Exercises `PermissionPlugin.before_tool_callback`'s `_scope_gate`: a path tool
targeting OUTSIDE the project ∪ granted dirs prompts to grant (Allow folder /
once / deny) instead of hitting the sandbox hard-deny, and the grant actually
widens the effective scope. Also covers the protected-path floor and the
web-mode (feature-off) parity.

Run: `.venv/bin/python tests/test_grant_flow.py`
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, ClassVar, Optional

os.environ["ADK_CC_DESKTOP"] = "1"
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
# Isolate the credential-store protected patterns to a temp data dir.
_TMP_DATA = tempfile.mkdtemp(prefix="adkcc-grant-")
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP_DATA

from pydantic import BaseModel

from adk_cc.permissions.modes import PermissionMode
from adk_cc.permissions.settings import SettingsHierarchy
from adk_cc.plugins.permissions import PermissionPlugin
from adk_cc.sandbox.workspace import (
    WorkspaceRoot,
    _STATE_KEY,
    list_granted_roots,
)
from adk_cc.tools.base import AdkCcTool, ToolMeta


# --- Fakes ----------------------------------------------------------


class _PathArgs(BaseModel):
    path: str = ""
    content: str = ""


class _FakeWrite(AdkCcTool):
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="write_file", is_read_only=False, is_concurrency_safe=False,
        is_destructive=True,
    )
    input_model: ClassVar[type[BaseModel]] = _PathArgs
    description: ClassVar[str] = "fake write"

    async def _execute(self, args: BaseModel, ctx: Any) -> dict:
        return {"status": "ok"}


class _FakeRead(AdkCcTool):
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="read_file", is_read_only=True, is_concurrency_safe=True,
    )
    input_model: ClassVar[type[BaseModel]] = _PathArgs
    description: ClassVar[str] = "fake read"

    async def _execute(self, args: BaseModel, ctx: Any) -> dict:
        return {"status": "ok"}


class _Actions:
    def __init__(self) -> None:
        self.skip_summarization = False


class _Ctx:
    def __init__(self, *, project: str, mode: str = "default",
                 confirmation: Optional[Any] = None, state: Optional[dict] = None):
        self.state = {
            _STATE_KEY: WorkspaceRoot(tenant_id="local", session_id="s", abs_path=project),
            "permission_mode": mode,
        }
        if state:
            self.state.update(state)
        self.tool_confirmation = confirmation
        self.function_call_id = "call-1"
        self.actions = _Actions()
        self.requested: list[dict] = []

    def request_confirmation(self, *, hint=None, payload=None) -> None:
        self.requested.append({"hint": hint, "payload": payload})


class _Confirm:
    def __init__(self, *, payload: Optional[Any] = None, confirmed: bool = False):
        self.payload = payload
        self.confirmed = confirmed
        self.hint = ""


def _plugin() -> PermissionPlugin:
    return PermissionPlugin(SettingsHierarchy([]), default_mode=PermissionMode.DEFAULT)


def _call(plugin, tool, args, ctx):
    return asyncio.run(
        plugin.before_tool_callback(tool=tool, tool_args=args, tool_context=ctx)
    )


# --- Tests ----------------------------------------------------------


def test_out_of_scope_write_prompts_to_grant() -> None:
    """An out-of-project write pauses with a single_select grant prompt whose
    options are grant_folder / grant_once / grant_deny."""
    proj = os.path.realpath(tempfile.mkdtemp(prefix="proj-"))
    outside = os.path.realpath(tempfile.mkdtemp(prefix="outside-"))
    ctx = _Ctx(project=proj)
    res = _call(_plugin(), _FakeWrite(), {"path": f"{outside}/x.txt", "content": "hi"}, ctx)
    assert res["status"] == "needs_confirmation", res
    assert len(ctx.requested) == 1
    opts = ctx.requested[0]["payload"]["options"]
    ids = [o["id"] for o in opts]
    assert ids == ["grant_folder", "grant_once", "grant_deny"], ids
    assert ctx.actions.skip_summarization is True
    print("OK test_out_of_scope_write_prompts_to_grant")


def test_scope_prompt_fires_even_in_bypass() -> None:
    """Scope expansion needs consent even under bypassPermissions (the gate runs
    before the mode short-circuit)."""
    proj = os.path.realpath(tempfile.mkdtemp(prefix="proj-"))
    outside = os.path.realpath(tempfile.mkdtemp(prefix="outside-"))
    ctx = _Ctx(project=proj, mode="bypassPermissions")
    res = _call(_plugin(), _FakeWrite(), {"path": f"{outside}/x.txt"}, ctx)
    assert res["status"] == "needs_confirmation", res
    print("OK test_scope_prompt_fires_even_in_bypass")


def test_grant_folder_widens_scope_and_stops_prompting() -> None:
    """grant_folder adds the parent to granted roots + <dir>/* allow rules; a
    second file in that dir is then in-scope and auto-allowed (no prompt)."""
    proj = os.path.realpath(tempfile.mkdtemp(prefix="proj-"))
    outside = os.path.realpath(tempfile.mkdtemp(prefix="outside-"))
    plugin = _plugin()
    ctx = _Ctx(project=proj)
    # Second HITL call: user chose grant_folder.
    conf = _Confirm(payload={"chose_id": "grant_folder"})
    ctx.tool_confirmation = conf
    res = _call(plugin, _FakeWrite(), {"path": f"{outside}/a.txt"}, ctx)
    assert res is None, res  # None → let the tool run
    assert outside in list_granted_roots(ctx), list_granted_roots(ctx)

    # A DIFFERENT file in the granted dir, fresh call (no confirmation) → allowed.
    ctx.tool_confirmation = None
    res2 = _call(plugin, _FakeWrite(), {"path": f"{outside}/b.txt"}, ctx)
    assert res2 is None, res2  # in-scope + <dir>/* allow rule → allow, no prompt
    assert ctx.requested == []  # no new prompt
    print("OK test_grant_folder_widens_scope_and_stops_prompting")


def test_grant_once_is_single_use() -> None:
    """grant_once allows exactly one op; the after_tool_callback clears it, so a
    later op re-prompts."""
    proj = os.path.realpath(tempfile.mkdtemp(prefix="proj-"))
    outside = os.path.realpath(tempfile.mkdtemp(prefix="outside-"))
    plugin = _plugin()
    ctx = _Ctx(project=proj)
    ctx.tool_confirmation = _Confirm(payload={"chose_id": "grant_once"})
    tool = _FakeWrite()
    res = _call(plugin, tool, {"path": f"{outside}/a.txt"}, ctx)
    assert res is None
    assert os.path.join(outside, "a.txt") in list_granted_roots(ctx)
    # Simulate the tool finishing → after_tool_callback consumes the one-shot.
    asyncio.run(plugin.after_tool_callback(
        tool=tool, tool_args={"path": f"{outside}/a.txt"}, tool_context=ctx, result={}))
    assert os.path.join(outside, "a.txt") not in list_granted_roots(ctx)
    print("OK test_grant_once_is_single_use")


def test_grant_deny_denies() -> None:
    proj = os.path.realpath(tempfile.mkdtemp(prefix="proj-"))
    outside = os.path.realpath(tempfile.mkdtemp(prefix="outside-"))
    ctx = _Ctx(project=proj)
    ctx.tool_confirmation = _Confirm(payload={"chose_id": "grant_deny"})
    res = _call(_plugin(), _FakeWrite(), {"path": f"{outside}/a.txt"}, ctx)
    assert res["status"] == "permission_denied_by_user", res
    print("OK test_grant_deny_denies")


def test_read_out_of_scope_also_prompts() -> None:
    """Reads share the scope: an out-of-project read prompts to grant (it is NOT
    silently allowed by the read-only permission path)."""
    proj = os.path.realpath(tempfile.mkdtemp(prefix="proj-"))
    outside = os.path.realpath(tempfile.mkdtemp(prefix="outside-"))
    ctx = _Ctx(project=proj)
    res = _call(_plugin(), _FakeRead(), {"path": f"{outside}/notes.md"}, ctx)
    assert res["status"] == "needs_confirmation", res
    print("OK test_read_out_of_scope_also_prompts")


def test_in_project_write_is_normal_flow() -> None:
    """In-project writes bypass the scope gate → the normal destructive ask
    (DEFAULT) / auto (bypass)."""
    proj = os.path.realpath(tempfile.mkdtemp(prefix="proj-"))
    # DEFAULT → destructive ask (a grant prompt would be single_select with
    # grant_* ids; the destructive prompt uses allow_once/allow_always).
    ctx = _Ctx(project=proj, mode="default")
    res = _call(_plugin(), _FakeWrite(), {"path": "src/a.txt"}, ctx)
    assert res["status"] == "needs_confirmation"
    ids = [o["id"] for o in ctx.requested[0]["payload"]["options"]]
    assert "allow_once" in ids and "grant_folder" not in ids, ids
    # bypass → auto-allow, no prompt.
    ctx2 = _Ctx(project=proj, mode="bypassPermissions")
    assert _call(_plugin(), _FakeWrite(), {"path": "src/a.txt"}, ctx2) is None
    print("OK test_in_project_write_is_normal_flow")


def test_secret_store_denied_not_granted() -> None:
    """An out-of-scope path that is protected secret material is DENIED, never
    offered as a grant."""
    proj = os.path.realpath(tempfile.mkdtemp(prefix="proj-"))
    ctx = _Ctx(project=proj)
    secret = os.path.join(_TMP_DATA, "secrets", "api.key")
    res = _call(_plugin(), _FakeRead(), {"path": secret}, ctx)
    assert res is not None and res["status"] == "permission_denied", res
    assert ctx.requested == []  # no grant prompt for secret material
    print("OK test_secret_store_denied_not_granted")


def test_web_mode_no_grant_prompt() -> None:
    """With is_desktop() False the feature is fully off: no grant prompt (the
    hard sandbox handles out-of-workspace)."""
    proj = os.path.realpath(tempfile.mkdtemp(prefix="proj-"))
    outside = os.path.realpath(tempfile.mkdtemp(prefix="outside-"))
    os.environ["ADK_CC_DESKTOP"] = "0"
    try:
        ctx = _Ctx(project=proj)
        res = _call(_plugin(), _FakeRead(), {"path": f"{outside}/x"}, ctx)
        # read_file is read-only → decide() allows; gate 2 (runtime) would deny.
        assert res is None, res
        assert ctx.requested == []
    finally:
        os.environ["ADK_CC_DESKTOP"] = "1"
    print("OK test_web_mode_no_grant_prompt")


def main() -> None:
    test_out_of_scope_write_prompts_to_grant()
    test_scope_prompt_fires_even_in_bypass()
    test_grant_folder_widens_scope_and_stops_prompting()
    test_grant_once_is_single_use()
    test_grant_deny_denies()
    test_read_out_of_scope_also_prompts()
    test_in_project_write_is_normal_flow()
    test_secret_store_denied_not_granted()
    test_web_mode_no_grant_prompt()
    print("\nall grant-flow tests passed")


if __name__ == "__main__":
    main()
