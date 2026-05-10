"""Unit tests for the confirm/deny confirmation UX in `PermissionPlugin`.

Covers:
  - Call-site payload shape: structured `ConfirmPrompt` is sent via
    `request_confirmation(payload=...)`, mirroring the documented
    frontend protocol.
  - Resume paths:
      1. structured response with `chose_id="allow"` → run the tool
      2. structured response with `chose_id="deny"` → return
         permission_denied_by_user
      3. legacy frontend, `confirmed=True`, no payload → run the tool
      4. legacy frontend, `confirmed=False`, no payload → denied
      5. malformed payload (`chose_id="bogus"`) → falls back to
         `confirmed: bool`; doesn't crash

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
    confirm_deny_prompt,
)
from adk_cc.permissions.modes import PermissionMode
from adk_cc.permissions.settings import SettingsHierarchy
from adk_cc.plugins.permissions import PermissionPlugin, _read_choice_id
from adk_cc.tools.base import AdkCcTool, ToolMeta


# --- Fakes ----------------------------------------------------------


class _Args(BaseModel):
    command: str


class _FakeBashTool(AdkCcTool):
    """Stand-in AdkCcTool that decide() will classify as destructive.

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
    on the structured payload. Mirrors the attribute surface that
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


def _make_plugin() -> PermissionPlugin:
    # No explicit rules needed: `_FakeBashTool.meta.is_destructive=True`
    # makes `decide()` return "ask" via the destructive-tool fallback
    # in DEFAULT mode (engine.py step 4).
    return PermissionPlugin(SettingsHierarchy(), default_mode=PermissionMode.DEFAULT)


# --- Module-level helpers ------------------------------------------


def test_confirm_deny_prompt_shape() -> None:
    """`confirm_deny_prompt` produces the canonical two-option payload."""
    prompt = confirm_deny_prompt("run_bash", "destructive op")
    assert isinstance(prompt, ConfirmPrompt)
    assert prompt.style == "confirm_deny"
    assert prompt.title == "Confirm run_bash?"
    assert prompt.detail == "destructive op"
    assert len(prompt.options) == 2
    assert [o.id for o in prompt.options] == ["allow", "deny"]
    # Each option carries label + description for the frontend.
    for opt in prompt.options:
        assert isinstance(opt, ConfirmOption)
        assert opt.label and opt.description
    # Serializes cleanly — this is what hits the wire.
    dumped = prompt.model_dump()
    assert dumped["style"] == "confirm_deny"
    assert dumped["options"][0]["id"] == "allow"
    assert dumped["options"][1]["id"] == "deny"
    print("OK test_confirm_deny_prompt_shape")


def test_read_choice_id_helper() -> None:
    """The helper tolerates every kind of garbage."""
    assert _read_choice_id(_FakeConfirmation(payload={"chose_id": "allow"})) == "allow"
    assert _read_choice_id(_FakeConfirmation(payload={"chose_id": "deny"})) == "deny"
    assert _read_choice_id(_FakeConfirmation(payload=None)) is None
    assert _read_choice_id(_FakeConfirmation(payload={})) is None
    assert _read_choice_id(_FakeConfirmation(payload="not-a-dict")) is None
    assert _read_choice_id(_FakeConfirmation(payload={"chose_id": 123})) is None
    print("OK test_read_choice_id_helper")


# --- before_tool_callback paths ------------------------------------


def test_first_call_emits_structured_payload() -> None:
    """First invocation: plugin must call `request_confirmation` with a
    structured `ConfirmPrompt` payload AND set skip_summarization."""
    plugin = _make_plugin()
    tool = _FakeBashTool()
    ctx = _FakeToolContext()

    result = asyncio.run(
        plugin.before_tool_callback(
            tool=tool, tool_args={"command": "rm -rf /"}, tool_context=ctx
        )
    )

    # Returns a needs_confirmation dict so the model sees the gate.
    assert isinstance(result, dict), result
    assert result["status"] == "needs_confirmation", result

    # request_confirmation was called exactly once.
    assert len(ctx.requested) == 1, ctx.requested
    call = ctx.requested[0]

    # hint mirrors the legacy back-compat field.
    assert call["hint"]
    # payload is the structured ConfirmPrompt dump.
    payload = call["payload"]
    assert isinstance(payload, dict), payload
    assert payload["style"] == "confirm_deny"
    assert payload["title"] == "Confirm run_bash?"
    assert payload["detail"] == call["hint"]
    assert [o["id"] for o in payload["options"]] == ["allow", "deny"]

    # The skip_summarization flag is the linchpin keeping ADK's runner
    # from re-prompting the LLM before the user responds.
    assert ctx.actions.skip_summarization is True
    print("OK test_first_call_emits_structured_payload")


def test_resume_allow_via_payload() -> None:
    """Frontend submits `chose_id=allow` → plugin lets the tool run."""
    plugin = _make_plugin()
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
    print("OK test_resume_allow_via_payload")


def test_resume_deny_via_payload() -> None:
    """Frontend submits `chose_id=deny` → plugin short-circuits."""
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


def test_resume_malformed_payload_falls_back() -> None:
    """Garbage `chose_id` → falls back to `confirmed: bool`.

    Two sub-cases: with confirmed=True (allow) and confirmed=False (deny).
    Either way the plugin must not crash on the bogus id.
    """
    plugin = _make_plugin()
    tool = _FakeBashTool()

    # Bogus id, confirmed=True → denied. Garbage id is NOT silently
    # treated as allow — only the literal "allow" is allow. This matches
    # the call-site code: `chose_id == "allow"` first; only if chose_id
    # is None do we consult `confirmed`. So a non-None bogus id with
    # confirmed=True falls through to the denied branch. Defensive.
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

    # Non-string `chose_id` (e.g. integer) → helper returns None,
    # falls back cleanly to `confirmed: bool`.
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
    test_read_choice_id_helper()
    test_first_call_emits_structured_payload()
    test_resume_allow_via_payload()
    test_resume_deny_via_payload()
    test_resume_legacy_confirmed_true()
    test_resume_legacy_confirmed_false()
    test_resume_malformed_payload_falls_back()
    print("\nall permissions-confirmation tests passed")


if __name__ == "__main__":
    main()
