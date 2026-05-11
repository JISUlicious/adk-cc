"""End-to-end test for ConfirmationFormUiPlugin wired into the real ADK
plugin chain alongside PermissionPlugin.

What this proves over the unit tests:

  - Outbound rewrite reaches the actual event stream — adk web would
    see the sentinel name `adk_cc_confirmation_form` with a populated
    `response_schema` on the wire, and the bundled UI's render path
    would take the form widget instead of the binary confirmation
    widget.

  - Inbound reshape integrates with ADK's existing
    `_RequestConfirmationLlmRequestProcessor`. Submitting a
    `{choice: "allow_once"}` response under the sentinel name causes
    ADK to resume the gated tool exactly as if a regular
    `ToolConfirmation(confirmed=True, payload={chose_id="allow_once"})`
    had been submitted — i.e. the wrapped plugin chain re-runs the
    destructive tool.

Run: `.venv/bin/python tests/e2e_confirmation_form_ui.py`
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncGenerator, ClassVar

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")

from google.adk.agents.llm_agent import LlmAgent
from google.adk.events.event import Event
from google.adk.flows.llm_flows.functions import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel, Field

from adk_cc.permissions.modes import PermissionMode
from adk_cc.permissions.settings import SettingsHierarchy
from adk_cc.plugins.confirmation_form_ui import (
    CONFIRMATION_FORM_FUNCTION_CALL_NAME,
    ConfirmationFormUiPlugin,
)
from adk_cc.plugins.permissions import PermissionPlugin
from adk_cc.tools.base import AdkCcTool, ToolMeta


# --- Fakes ----------------------------------------------------------


class _ScriptedLlm(BaseLlm):
    model: str = "fake/scripted-test"
    responses: list[LlmResponse] = Field(default_factory=list)
    calls_made: int = 0

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"fake/.*"]

    async def generate_content_async(
        self, llm_request, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if not self.responses:
            raise RuntimeError(
                f"_ScriptedLlm queue empty on call #{self.calls_made + 1}"
            )
        self.calls_made += 1
        yield self.responses.pop(0)


def _tool_call_response(call_id: str, name: str, args: dict) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(id=call_id, name=name, args=args)
                )
            ],
        ),
        partial=False,
    )


def _text_response(text: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        partial=False,
    )


class _Args(BaseModel):
    command: str


class _FakeBashTool(AdkCcTool):
    meta: ClassVar[ToolMeta] = ToolMeta(
        name="run_bash",
        is_read_only=False,
        is_concurrency_safe=False,
        is_destructive=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = "fake bash"
    invocations: ClassVar[list[dict]] = []

    async def _execute(self, args: BaseModel, ctx: Any) -> dict:
        type(self).invocations.append({"command": args.command})
        return {"status": "ok", "stdout": f"ran: {args.command}"}


def _find_form_call(events: list[Event]) -> tuple[str, dict]:
    """Locate the rewritten confirmation event — name should be the
    sentinel and args should carry response_schema."""
    for ev in events:
        for fc in ev.get_function_calls():
            if fc.name == CONFIRMATION_FORM_FUNCTION_CALL_NAME:
                return fc.id, fc.args
    raise AssertionError(
        f"no {CONFIRMATION_FORM_FUNCTION_CALL_NAME} call found; "
        f"function-call names seen: "
        f"{[fc.name for e in events for fc in e.get_function_calls()]}"
    )


def _user_form_submission(call_id: str, choice: str) -> types.Content:
    """Mimic the bundled UI's onSend for the form-widget path.

    The current schema produces one boolean field per option (so the UI
    renders N checkboxes); ticking one yields `{<chose_id>: true, ...}`.
    """
    all_ids = ["allow_once", "allow_always", "deny"]
    response = {oid: (oid == choice) for oid in all_ids}
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=call_id,
                    name=CONFIRMATION_FORM_FUNCTION_CALL_NAME,
                    response=response,
                )
            )
        ],
    )


def _build_runner() -> tuple[InMemoryRunner, _ScriptedLlm]:
    _FakeBashTool.invocations = []
    llm = _ScriptedLlm(
        responses=[
            _tool_call_response("orig-1", "run_bash", {"command": "git status"}),
            _text_response("done"),
        ]
    )
    tool = _FakeBashTool()
    agent = LlmAgent(
        name="test_agent",
        model=llm,
        instruction="Test agent.",
        tools=[tool],
    )
    plugins = [
        PermissionPlugin(SettingsHierarchy(), default_mode=PermissionMode.DEFAULT),
        ConfirmationFormUiPlugin(),
    ]
    runner = InMemoryRunner(agent=agent, plugins=plugins, app_name="e2e-form")
    return runner, llm


async def _create_session(runner: InMemoryRunner, user_id: str, session_id: str) -> None:
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )


# --- Tests ----------------------------------------------------------


async def test_outbound_event_has_sentinel_name_and_schema() -> None:
    """First invocation gates → the event in the stream is renamed and
    carries `response_schema` with the three chose_ids in `enum`."""
    runner, _ = _build_runner()
    await _create_session(runner, "alice", "s-outbound")

    events: list[Event] = []
    async for ev in runner.run_async(
        user_id="alice",
        session_id="s-outbound",
        new_message=types.Content(
            role="user", parts=[types.Part(text="run git status")]
        ),
    ):
        events.append(ev)

    call_id, args = _find_form_call(events)
    assert call_id, call_id
    schema = args.get("response_schema")
    assert isinstance(schema, dict) and schema.get("type") == "object"
    # One boolean property per option (so the bundled form renders one
    # checkbox per choice — enum strings would render as a text input).
    props = schema["properties"]
    assert list(props.keys()) == ["allow_once", "allow_always", "deny"], list(props)
    for key in props:
        assert props[key]["type"] == "boolean", props[key]

    # No event should still have the original adk_request_confirmation
    # name — the rewrite happens on every confirmation event.
    legacy_named = [
        fc.name
        for ev in events
        for fc in ev.get_function_calls()
        if fc.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    ]
    assert legacy_named == [], legacy_named

    # Tool hasn't been invoked yet.
    assert _FakeBashTool.invocations == [], _FakeBashTool.invocations
    print("OK test_outbound_event_has_sentinel_name_and_schema")


async def test_inbound_form_submission_resumes_tool() -> None:
    """Submit `{choice: "allow_once"}` under the sentinel name; verify
    ADK's existing confirmation resume processor re-runs the gated
    tool."""
    runner, _ = _build_runner()
    await _create_session(runner, "alice", "s-inbound-allow")

    # Invocation 1: gate.
    events1: list[Event] = []
    async for ev in runner.run_async(
        user_id="alice",
        session_id="s-inbound-allow",
        new_message=types.Content(
            role="user", parts=[types.Part(text="run git status")]
        ),
    ):
        events1.append(ev)
    call_id, _ = _find_form_call(events1)

    # Invocation 2: submit form answer.
    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-inbound-allow",
        new_message=_user_form_submission(call_id, "allow_once"),
    ):
        pass

    # Destructive tool ran with the original args.
    assert _FakeBashTool.invocations == [{"command": "git status"}], _FakeBashTool.invocations
    print("OK test_inbound_form_submission_resumes_tool")


async def test_inbound_deny_choice_blocks_tool() -> None:
    runner, _ = _build_runner()
    await _create_session(runner, "alice", "s-inbound-deny")

    events1: list[Event] = []
    async for ev in runner.run_async(
        user_id="alice",
        session_id="s-inbound-deny",
        new_message=types.Content(
            role="user", parts=[types.Part(text="run git status")]
        ),
    ):
        events1.append(ev)
    call_id, _ = _find_form_call(events1)

    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-inbound-deny",
        new_message=_user_form_submission(call_id, "deny"),
    ):
        pass

    # Destructive tool should NOT have run.
    assert _FakeBashTool.invocations == [], _FakeBashTool.invocations
    print("OK test_inbound_deny_choice_blocks_tool")


def _build_runner_multi_tool() -> tuple[InMemoryRunner, _ScriptedLlm]:
    """Two run_bash function_calls in the FIRST LLM response so the gate
    fires twice in the same turn."""
    _FakeBashTool.invocations = []
    llm = _ScriptedLlm(
        responses=[
            LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(function_call=types.FunctionCall(
                            id="orig-1", name="run_bash",
                            args={"command": "git status"},
                        )),
                        types.Part(function_call=types.FunctionCall(
                            id="orig-2", name="run_bash",
                            args={"command": "git diff"},
                        )),
                    ],
                ),
                partial=False,
            ),
            _text_response("done"),
        ]
    )
    agent = LlmAgent(
        name="test_agent",
        model=llm,
        instruction="Test agent.",
        tools=[_FakeBashTool()],
    )
    plugins = [
        PermissionPlugin(SettingsHierarchy(), default_mode=PermissionMode.DEFAULT),
        ConfirmationFormUiPlugin(),
    ]
    runner = InMemoryRunner(agent=agent, plugins=plugins, app_name="e2e-form-multi")
    return runner, llm


def _all_form_call_ids(events: list[Event]) -> list[str]:
    """Every confirmation wrapper id seen in the event stream, in order."""
    ids: list[str] = []
    for ev in events:
        for fc in ev.get_function_calls():
            if fc.name == CONFIRMATION_FORM_FUNCTION_CALL_NAME:
                ids.append(fc.id)
    return ids


async def test_multi_tool_first_submission_defers_no_tool_run() -> None:
    """Two gated tool calls in one turn → two confirmation widgets.
    Submitting widget 1 must NOT resume tool 1 yet — the plugin defers
    until all are submitted. After the first submit, _FakeBashTool
    has zero invocations."""
    runner, _ = _build_runner_multi_tool()
    await _create_session(runner, "alice", "s-multi-defer")

    # Turn 1: model emits two run_bash → both gated.
    events1: list[Event] = []
    async for ev in runner.run_async(
        user_id="alice",
        session_id="s-multi-defer",
        new_message=types.Content(
            role="user", parts=[types.Part(text="run both")]
        ),
    ):
        events1.append(ev)
    wrap_ids = _all_form_call_ids(events1)
    assert len(wrap_ids) == 2, wrap_ids

    # Submit only widget 1.
    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-multi-defer",
        new_message=_user_form_submission(wrap_ids[0], "allow_once"),
    ):
        pass

    # Neither tool should have run yet — first submission is deferred.
    assert _FakeBashTool.invocations == [], (
        f"expected no tool runs after only 1 of 2 confirmations submitted, "
        f"got {_FakeBashTool.invocations}"
    )
    print("OK test_multi_tool_first_submission_defers_no_tool_run")


async def test_multi_tool_final_submission_bundles_and_resumes_all() -> None:
    """After submitting widget 2, both tools resume in one pass (modulo
    the deny decision on widget 1 vs widget 2)."""
    runner, _ = _build_runner_multi_tool()
    await _create_session(runner, "alice", "s-multi-bundle")

    events1: list[Event] = []
    async for ev in runner.run_async(
        user_id="alice",
        session_id="s-multi-bundle",
        new_message=types.Content(
            role="user", parts=[types.Part(text="run both")]
        ),
    ):
        events1.append(ev)
    wrap_ids = _all_form_call_ids(events1)
    assert len(wrap_ids) == 2

    # Submit widget 1 (deferred).
    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-multi-bundle",
        new_message=_user_form_submission(wrap_ids[0], "allow_once"),
    ):
        pass
    assert _FakeBashTool.invocations == []

    # Submit widget 2 — final submission triggers the bundle and ADK's
    # resume processor runs both tools.
    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-multi-bundle",
        new_message=_user_form_submission(wrap_ids[1], "allow_always"),
    ):
        pass

    # BOTH tools should have run now.
    commands = sorted(d["command"] for d in _FakeBashTool.invocations)
    assert commands == ["git diff", "git status"], commands
    print("OK test_multi_tool_final_submission_bundles_and_resumes_all")


async def test_multi_tool_mixed_allow_and_deny_bundles() -> None:
    """Deny one, allow the other. Final submission bundles both — the
    allow tool runs, the deny tool surfaces a permission_denied_by_user
    response without running."""
    runner, _ = _build_runner_multi_tool()
    await _create_session(runner, "alice", "s-multi-mixed")

    events1: list[Event] = []
    async for ev in runner.run_async(
        user_id="alice",
        session_id="s-multi-mixed",
        new_message=types.Content(
            role="user", parts=[types.Part(text="run both")]
        ),
    ):
        events1.append(ev)
    wrap_ids = _all_form_call_ids(events1)

    # Deny widget 1 (will not run).
    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-multi-mixed",
        new_message=_user_form_submission(wrap_ids[0], "deny"),
    ):
        pass
    assert _FakeBashTool.invocations == []

    # Allow widget 2 — bundle fires, denied tool stays denied, allowed runs.
    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-multi-mixed",
        new_message=_user_form_submission(wrap_ids[1], "allow_once"),
    ):
        pass

    commands = [d["command"] for d in _FakeBashTool.invocations]
    # Only the second (allowed) ran.
    assert commands == ["git diff"], commands
    print("OK test_multi_tool_mixed_allow_and_deny_bundles")


# --- Driver ---------------------------------------------------------


async def main() -> None:
    await test_outbound_event_has_sentinel_name_and_schema()
    await test_inbound_form_submission_resumes_tool()
    await test_inbound_deny_choice_blocks_tool()
    await test_multi_tool_first_submission_defers_no_tool_run()
    await test_multi_tool_final_submission_bundles_and_resumes_all()
    await test_multi_tool_mixed_allow_and_deny_bundles()
    print("\nall e2e confirmation_form_ui tests passed")


if __name__ == "__main__":
    asyncio.run(main())
