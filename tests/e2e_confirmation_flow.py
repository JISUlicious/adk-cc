"""End-to-end test for the structured tool-confirmation flow.

Unlike `test_permissions_confirmation.py` (which fakes `ToolContext`),
this drives ADK's real runtime: `InMemoryRunner` + `LlmAgent` +
`PermissionPlugin`, with a `FakeLlm` that scripts the model's tool-call
emissions. The actual ADK plugin chain runs, ADK's
`generate_request_confirmation_event` actually fires, and the
resumption path through `_RequestConfirmationLlmRequestProcessor` is
exercised end-to-end.

What this verifies that the unit tests don't:
  - `tool_context.request_confirmation(payload=...)` actually surfaces
    as a `requested_tool_confirmations` entry on the function_response
    event, which then becomes an `adk_request_confirmation` function
    call event with our structured payload visible on the wire.
  - Submitting a `ToolConfirmation` back as a user function_response
    actually reaches the plugin's resume branch with the payload intact.
  - After `allow_always`, ADK's plugin chain auto-allows the same op
    on the NEXT user-initiated invocation (the session rule we injected
    is consulted by `decide()` under real dispatch).
  - The denial path returns a tool result the LLM can see.

Run: `.venv/bin/python tests/e2e_confirmation_flow.py`
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, AsyncGenerator, ClassVar

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")

from google.adk.agents.llm_agent import LlmAgent
from google.adk.events.event import Event
from google.adk.flows.llm_flows.functions import (
    REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
)
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.genai import types
from pydantic import BaseModel, Field

from adk_cc.permissions.modes import PermissionMode
from adk_cc.permissions.rules import RuleBehavior, RuleSource
from adk_cc.permissions.settings import SettingsHierarchy
from adk_cc.plugins.permissions import PermissionPlugin
from adk_cc.tools.base import AdkCcTool, ToolMeta


# --- Fake LLM -------------------------------------------------------


class _ScriptedLlm(BaseLlm):
    """Scripted LLM: yields the next LlmResponse from `responses` per call.

    The Runner calls `generate_content_async` once per LLM turn. Tests
    queue responses in order. Each response is fully-formed
    (non-streaming, `partial=False`) and is yielded as the only chunk.
    """

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
                f"_ScriptedLlm queue empty on call #{self.calls_made + 1} — "
                "the test under-queued responses."
            )
        self.calls_made += 1
        resp = self.responses.pop(0)
        yield resp


def _tool_call_response(call_id: str, name: str, args: dict) -> LlmResponse:
    """LlmResponse with a single function_call part."""
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
    """LlmResponse with a single text part — final reply for the turn."""
    return LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(text=text)]
        ),
        partial=False,
    )


# --- Destructive tool under test ------------------------------------


class _Args(BaseModel):
    command: str


class _FakeBashTool(AdkCcTool):
    """Stand-in destructive AdkCcTool — triggers the gate on every call."""

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


# --- Helpers --------------------------------------------------------


def _find_request_confirmation_call(events: list[Event]) -> tuple[str, dict]:
    """Find the `adk_request_confirmation` function_call event ADK emits when
    a tool requested confirmation. Returns (call_id, args_dict)."""
    for ev in events:
        for fc in ev.get_function_calls():
            if fc.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME:
                return fc.id, fc.args
    raise AssertionError(
        f"no {REQUEST_CONFIRMATION_FUNCTION_CALL_NAME} call found in {len(events)} events; "
        f"event authors: {[e.author for e in events]}"
    )


def _confirmation_message(call_id: str, payload: dict) -> types.Content:
    """Build the user-side function_response that submits a confirmation."""
    confirmation = ToolConfirmation(payload=payload)
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=call_id,
                    name=REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
                    response=confirmation.model_dump(),
                )
            )
        ],
    )


async def _run_once(
    runner: InMemoryRunner,
    *,
    user_id: str,
    session_id: str,
    new_message: types.Content,
) -> list[Event]:
    """Drive one `run_async` to completion; collect events."""
    events: list[Event] = []
    async for ev in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=new_message,
    ):
        events.append(ev)
    return events


def _build_runner(
    *, llm_responses: list[LlmResponse], settings: SettingsHierarchy
) -> tuple[InMemoryRunner, PermissionPlugin]:
    """Spin up an InMemoryRunner wired with a scripted LLM + permission plugin."""
    _FakeBashTool.invocations = []  # reset between tests
    llm = _ScriptedLlm(responses=list(llm_responses))
    tool = _FakeBashTool()
    agent = LlmAgent(
        name="test_agent",
        model=llm,
        instruction="Test agent.",
        tools=[tool],
    )
    plugin = PermissionPlugin(settings, default_mode=PermissionMode.DEFAULT)
    runner = InMemoryRunner(agent=agent, plugins=[plugin], app_name="e2e-conf")
    return runner, plugin


async def _create_session(runner: InMemoryRunner, user_id: str, session_id: str) -> None:
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )


# --- Tests ----------------------------------------------------------


async def test_allow_once_end_to_end() -> None:
    """First invocation gates; second submits `allow_once`; tool runs once."""
    settings = SettingsHierarchy()
    runner, _ = _build_runner(
        llm_responses=[
            _tool_call_response("orig-1", "run_bash", {"command": "git status"}),
            _text_response("done"),
        ],
        settings=settings,
    )
    await _create_session(runner, "alice", "s-allow-once")

    # Invocation 1: user asks; LLM emits function_call; plugin gates.
    events1 = await _run_once(
        runner,
        user_id="alice",
        session_id="s-allow-once",
        new_message=types.Content(
            role="user", parts=[types.Part(text="run git status")]
        ),
    )
    conf_call_id, conf_args = _find_request_confirmation_call(events1)

    # Verify the payload on the wire carries our structured ConfirmPrompt.
    tc_dict = conf_args["toolConfirmation"]
    assert isinstance(tc_dict, dict), tc_dict
    payload = tc_dict.get("payload")
    assert isinstance(payload, dict), payload
    assert payload["style"] == "single_select", payload
    assert [o["id"] for o in payload["options"]] == [
        "allow_once",
        "allow_always",
        "deny",
    ], payload
    # Subject (the command) is in the title so concurrent gated calls
    # for the same tool can be distinguished by the operator.
    assert payload["title"] == "Confirm run_bash: git status?", payload

    # Tool should not have been invoked yet.
    assert _FakeBashTool.invocations == [], _FakeBashTool.invocations

    # Invocation 2: user submits allow_once → tool runs → LLM emits final text.
    events2 = await _run_once(
        runner,
        user_id="alice",
        session_id="s-allow-once",
        new_message=_confirmation_message(conf_call_id, {"chose_id": "allow_once"}),
    )

    # Tool ran exactly once with the right args.
    assert _FakeBashTool.invocations == [{"command": "git status"}], _FakeBashTool.invocations

    # No session rule was injected.
    assert settings.all_rules() == [], settings.all_rules()

    # The second invocation reached a final model text.
    final_texts = [
        p.text
        for ev in events2
        for p in (ev.content.parts if ev.content else [])
        if p.text
    ]
    assert any("done" in t for t in final_texts), final_texts
    print("OK test_allow_once_end_to_end")


async def test_allow_always_injects_session_rule_and_skips_re_ask() -> None:
    """`allow_always` injects the session rule; a subsequent identical
    user request is auto-allowed by the plugin with NO second prompt."""
    settings = SettingsHierarchy()
    runner, plugin = _build_runner(
        llm_responses=[
            _tool_call_response("orig-1", "run_bash", {"command": "git status"}),
            _text_response("done 1"),
            _tool_call_response("orig-2", "run_bash", {"command": "git status"}),
            _text_response("done 2"),
        ],
        settings=settings,
    )
    await _create_session(runner, "alice", "s-allow-always")

    # Invocation 1: gate.
    events1 = await _run_once(
        runner,
        user_id="alice",
        session_id="s-allow-always",
        new_message=types.Content(
            role="user", parts=[types.Part(text="run git status")]
        ),
    )
    conf_call_id, _ = _find_request_confirmation_call(events1)
    assert _FakeBashTool.invocations == []

    # Invocation 2: submit allow_always → tool runs → session rule injected.
    await _run_once(
        runner,
        user_id="alice",
        session_id="s-allow-always",
        new_message=_confirmation_message(conf_call_id, {"chose_id": "allow_always"}),
    )
    assert _FakeBashTool.invocations == [{"command": "git status"}]
    # The runtime rules land in session state now (not the in-memory
    # hierarchy); read through the session DB. Two rules per click:
    # literal + broadened (per `compute_allow_always_rule_contents`).
    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="alice", session_id="s-allow-always"
    )
    raw = session.state.get("adk_cc_allow_rules") or []
    assert len(raw) == 2, raw
    for r in raw:
        assert r["source"] == RuleSource.SESSION.value
        assert r["behavior"] == RuleBehavior.ALLOW.value
        assert r["tool_name"] == "run_bash"
    assert raw[0]["rule_content"] == "git status"     # literal
    assert raw[1]["rule_content"] == "git status *"   # broadened
    # The static hierarchy stays untouched — only state mutates.
    assert settings.all_rules() == []

    # Invocation 3: user asks again; LLM emits same function_call; plugin
    # checks the state-backed rule and lets it through with NO gate.
    events3 = await _run_once(
        runner,
        user_id="alice",
        session_id="s-allow-always",
        new_message=types.Content(
            role="user", parts=[types.Part(text="run it again")]
        ),
    )

    # NO request_confirmation event should have been emitted.
    any_conf = any(
        fc.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
        for ev in events3
        for fc in ev.get_function_calls()
    )
    assert not any_conf, "second call should not gate after allow_always"

    # Tool ran a second time.
    assert _FakeBashTool.invocations == [
        {"command": "git status"},
        {"command": "git status"},
    ], _FakeBashTool.invocations
    print("OK test_allow_always_injects_session_rule_and_skips_re_ask")


async def test_deny_short_circuits_and_surfaces_to_model() -> None:
    """`deny` short-circuits the tool. The function_response shows the
    denial; the model emits a final text acknowledging it."""
    settings = SettingsHierarchy()
    runner, _ = _build_runner(
        llm_responses=[
            _tool_call_response("orig-1", "run_bash", {"command": "rm -rf /"}),
            _text_response("acknowledged: denied"),
        ],
        settings=settings,
    )
    await _create_session(runner, "alice", "s-deny")

    events1 = await _run_once(
        runner,
        user_id="alice",
        session_id="s-deny",
        new_message=types.Content(
            role="user", parts=[types.Part(text="please rm -rf /")]
        ),
    )
    conf_call_id, _ = _find_request_confirmation_call(events1)
    assert _FakeBashTool.invocations == []

    events2 = await _run_once(
        runner,
        user_id="alice",
        session_id="s-deny",
        new_message=_confirmation_message(conf_call_id, {"chose_id": "deny"}),
    )

    # Tool should NOT have run.
    assert _FakeBashTool.invocations == [], _FakeBashTool.invocations
    # No session rule was injected.
    assert settings.all_rules() == [], settings.all_rules()

    # Somewhere in invocation 2 is a function_response carrying our
    # `permission_denied_by_user` status — the model sees this.
    denied_seen = False
    for ev in events2:
        for fr in ev.get_function_responses():
            resp = fr.response
            if isinstance(resp, dict) and resp.get("status") == "permission_denied_by_user":
                denied_seen = True
    assert denied_seen, "expected permission_denied_by_user in function responses"
    print("OK test_deny_short_circuits_and_surfaces_to_model")


async def test_allow_always_does_not_broaden_to_different_command() -> None:
    """After allow_always on `git status`, asking for `git diff` still gates.
    Verifies the session rule's scope is exact-match, not tool-wide."""
    settings = SettingsHierarchy()
    runner, _ = _build_runner(
        llm_responses=[
            _tool_call_response("orig-1", "run_bash", {"command": "git status"}),
            _text_response("done 1"),
            _tool_call_response("orig-2", "run_bash", {"command": "git diff"}),
            _text_response("done 2"),
        ],
        settings=settings,
    )
    await _create_session(runner, "alice", "s-scope")

    # Invocation 1+2: allow_always on git status.
    events1 = await _run_once(
        runner,
        user_id="alice",
        session_id="s-scope",
        new_message=types.Content(
            role="user", parts=[types.Part(text="run git status")]
        ),
    )
    conf_id1, _ = _find_request_confirmation_call(events1)
    await _run_once(
        runner,
        user_id="alice",
        session_id="s-scope",
        new_message=_confirmation_message(conf_id1, {"chose_id": "allow_always"}),
    )

    # Invocation 3: ask for git diff. Plugin should STILL gate — the
    # session rule is keyed on the exact command "git status".
    events3 = await _run_once(
        runner,
        user_id="alice",
        session_id="s-scope",
        new_message=types.Content(
            role="user", parts=[types.Part(text="run git diff")]
        ),
    )
    # Should have a fresh request_confirmation call.
    conf_id2, _ = _find_request_confirmation_call(events3)
    assert conf_id2 != conf_id1
    # Tool was NOT run for git diff yet (still gating).
    assert _FakeBashTool.invocations == [
        {"command": "git status"},
    ], _FakeBashTool.invocations
    print("OK test_allow_always_does_not_broaden_to_different_command")


# --- Driver ---------------------------------------------------------


async def main() -> None:
    await test_allow_once_end_to_end()
    await test_allow_always_injects_session_rule_and_skips_re_ask()
    await test_deny_short_circuits_and_surfaces_to_model()
    await test_allow_always_does_not_broaden_to_different_command()
    print("\nall e2e confirmation-flow tests passed")


if __name__ == "__main__":
    asyncio.run(main())
