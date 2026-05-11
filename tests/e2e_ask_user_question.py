"""End-to-end test that `ask_user_question` pauses the agent loop AND
keeps the bundled `adk web` UI's response widget visible.

Two invariants:

  1. Loop pauses after the initial call (no LLM re-invocation until the
     user submits an answer). Verified by queuing exactly ONE LLM
     response on the scripted LLM — if the loop cascades, the queue
     empties and `_ScriptedLlm` raises loudly.

  2. No function_response event is built for the initial call. The
     bundled UI's response widget renders only when
     `needsResponse && !hasFunctionResponse(callId)` — so as soon as a
     function_response lands, the widget hides. `_execute` returning
     `None` lets ADK's long-running short-circuit (`functions.py:578`)
     skip the response-event build, keeping the call "pending" until
     the user actually submits.

Pause mechanism: `long_running_tool_ids` on the function-CALL event
(set by ADK from `is_long_running=True`), which alone makes the call
event `is_final_response()` → runner pauses.

Run: `.venv/bin/python tests/e2e_ask_user_question.py`
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncGenerator

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")

from google.adk.agents.llm_agent import LlmAgent
from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import Field

from adk_cc.tools.ask_user_question import AskUserQuestionTool


# --- Scripted LLM ---------------------------------------------------


class _ScriptedLlm(BaseLlm):
    """Yields the next queued LlmResponse per call; errors loudly if empty.

    Empty-queue error is the test's primary assertion vehicle: if the
    loop fails to pause, the runner re-invokes the LLM and this raises.
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
                "the loop did not pause when it should have, OR the test "
                "under-queued the resume path."
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


# --- Test fixtures --------------------------------------------------


def _build_runner(*, llm_responses: list[LlmResponse]) -> tuple[InMemoryRunner, _ScriptedLlm]:
    llm = _ScriptedLlm(responses=list(llm_responses))
    agent = LlmAgent(
        name="test_agent",
        model=llm,
        instruction="Test agent.",
        tools=[AskUserQuestionTool()],
    )
    runner = InMemoryRunner(agent=agent, app_name="e2e-ask")
    return runner, llm


async def _create_session(runner: InMemoryRunner, user_id: str, session_id: str) -> None:
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )


def _ask_args() -> dict:
    """Canonical ask_user_question payload — one question, two options."""
    return {
        "questions": [
            {
                "question": "Which path do you prefer?",
                "header": "Path",
                "options": [
                    {"label": "Refactor", "description": "Rewrite the inner loop."},
                    {"label": "Patch", "description": "Localized fix only."},
                ],
                "multi_select": False,
            }
        ]
    }


# --- Tests ----------------------------------------------------------


async def test_loop_pauses_after_ask_user_question() -> None:
    """One LLM response queued. After ask_user_question, the loop must
    pause — if it doesn't, _ScriptedLlm raises 'queue empty'."""
    runner, llm = _build_runner(
        llm_responses=[_tool_call_response("call-1", "ask_user_question", _ask_args())]
    )
    await _create_session(runner, "alice", "s-pause")

    events: list[Event] = []
    async for ev in runner.run_async(
        user_id="alice",
        session_id="s-pause",
        new_message=types.Content(
            role="user", parts=[types.Part(text="ask me which path")]
        ),
    ):
        events.append(ev)

    # The runner exited cleanly after a single LLM call. Without the fix,
    # _ScriptedLlm would have raised on the second call.
    assert llm.calls_made == 1, llm.calls_made
    print("OK test_loop_pauses_after_ask_user_question")


async def test_no_function_response_event_for_initial_call() -> None:
    """When `_execute` returns None, ADK skips the function_response
    event entirely — the call stays "pending" and the bundled UI's
    response widget remains visible (its render condition is
    `needsResponse && !hasFunctionResponse(callId)`)."""
    runner, _ = _build_runner(
        llm_responses=[_tool_call_response("call-1", "ask_user_question", _ask_args())]
    )
    await _create_session(runner, "alice", "s-no-resp")

    events: list[Event] = []
    async for ev in runner.run_async(
        user_id="alice",
        session_id="s-no-resp",
        new_message=types.Content(
            role="user", parts=[types.Part(text="ask me")]
        ),
    ):
        events.append(ev)

    # No event should carry a function_response for call-1.
    responses_for_call = [
        fr
        for ev in events
        for fr in ev.get_function_responses()
        if fr.id == "call-1"
    ]
    assert responses_for_call == [], (
        f"expected zero function_responses for the initial call; got "
        f"{[fr.response for fr in responses_for_call]}"
    )

    # The function-call event for call-1 must still be in the stream and
    # must carry long_running_tool_ids — that's what pauses the runner.
    call_events = [
        e for e in events
        if any(fc.id == "call-1" for fc in e.get_function_calls())
    ]
    assert call_events, "expected the ask_user_question function-call event"
    assert any(
        e.long_running_tool_ids and "call-1" in e.long_running_tool_ids
        for e in call_events
    ), "function-call event must carry long_running_tool_ids for the pause"
    print("OK test_no_function_response_event_for_initial_call")


async def test_resume_with_user_answer_continues_loop() -> None:
    """After the pause, submitting a function_response with the user's
    answer causes the LLM to be called again with the answer in context."""
    runner, llm = _build_runner(
        llm_responses=[
            _tool_call_response("call-1", "ask_user_question", _ask_args()),
            _text_response("got it: Refactor"),
        ]
    )
    await _create_session(runner, "alice", "s-resume")

    # Invocation 1: ask.
    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-resume",
        new_message=types.Content(
            role="user", parts=[types.Part(text="ask me")]
        ),
    ):
        pass
    assert llm.calls_made == 1, llm.calls_made

    # Invocation 2: user submits the answer as the FIRST function_response
    # for the call. With the new design there's no awaiting_user_input
    # event sitting in history — the user's answer is the call's only
    # response.
    answer = {"Which path do you prefer?": "Refactor"}
    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-resume",
        new_message=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="call-1",
                        name="ask_user_question",
                        response=answer,
                    )
                )
            ],
        ),
    ):
        pass
    # LLM was called a second time (the text response was consumed).
    assert llm.calls_made == 2, llm.calls_made
    print("OK test_resume_with_user_answer_continues_loop")


async def test_long_running_flag_propagates_to_event() -> None:
    """Sanity check on ADK behavior we rely on: the function-CALL event
    has the tool's call_id in `long_running_tool_ids`. This is what
    actually pauses the loop in the absence of a function_response."""
    runner, _ = _build_runner(
        llm_responses=[_tool_call_response("call-1", "ask_user_question", _ask_args())]
    )
    await _create_session(runner, "alice", "s-flag")

    events: list[Event] = []
    async for ev in runner.run_async(
        user_id="alice",
        session_id="s-flag",
        new_message=types.Content(
            role="user", parts=[types.Part(text="ask me")]
        ),
    ):
        events.append(ev)

    call_events = [e for e in events if e.get_function_calls()]
    assert call_events, "expected at least one function-call event"
    flagged = any(
        e.long_running_tool_ids and "call-1" in e.long_running_tool_ids
        for e in call_events
    )
    assert flagged, (
        "ADK should mark the function-CALL event with long_running_tool_ids "
        "for tools whose is_long_running=True"
    )
    print("OK test_long_running_flag_propagates_to_event")


# --- With AskUserQuestionUiHintPlugin in the chain ----------------


from adk_cc.plugins.ask_user_question_ui import AskUserQuestionUiHintPlugin


def _build_runner_with_form_plugin(
    *, llm_responses: list[LlmResponse]
) -> tuple[InMemoryRunner, _ScriptedLlm]:
    llm = _ScriptedLlm(responses=list(llm_responses))
    agent = LlmAgent(
        name="test_agent",
        model=llm,
        instruction="Test agent.",
        tools=[AskUserQuestionTool()],
    )
    runner = InMemoryRunner(
        agent=agent,
        plugins=[AskUserQuestionUiHintPlugin()],
        app_name="e2e-ask-form",
    )
    return runner, llm


def _multi_question_args() -> dict:
    """Two questions in one call: a single-select and a multi-select.
    Exercises mixed-question schema generation + reshape. AskOption
    requires both label and description, so every option carries a
    description (use empty string when not meaningful)."""
    return {
        "questions": [
            {
                "question": "Which DB?",
                "header": "DB",
                "options": [
                    {"label": "Postgres", "description": "Strong tooling"},
                    {"label": "MySQL", "description": "Wide support"},
                ],
                "multi_select": False,
            },
            {
                "question": "Tags?",
                "header": "Tags",
                "options": [
                    {"label": "urgent", "description": ""},
                    {"label": "cleanup", "description": ""},
                    {"label": "docs", "description": ""},
                ],
                "multi_select": True,
            },
        ]
    }


async def test_outbound_schema_uses_boolean_per_option_and_freeform() -> None:
    """With the plugin wired, the function-call event the bundled UI
    sees carries a `response_schema` with boolean checkbox properties
    per option plus a free-form string per question — the only shape
    bundled `adk web` actually renders as checkboxes (not enum dropdowns
    which it ignores)."""
    runner, _ = _build_runner_with_form_plugin(
        llm_responses=[
            _tool_call_response("call-1", "ask_user_question", _multi_question_args())
        ]
    )
    await _create_session(runner, "alice", "s-form-outbound")

    events: list[Event] = []
    async for ev in runner.run_async(
        user_id="alice",
        session_id="s-form-outbound",
        new_message=types.Content(
            role="user", parts=[types.Part(text="ask me two things")]
        ),
    ):
        events.append(ev)

    # Find the ask_user_question function-call.
    fc = None
    for ev in events:
        for f in ev.get_function_calls():
            if f.name == "ask_user_question":
                fc = f
                break
        if fc:
            break
    assert fc is not None, "no ask_user_question function-call event"
    schema = (fc.args or {}).get("response_schema")
    assert isinstance(schema, dict), schema
    assert schema["type"] == "object"
    props = schema["properties"]
    # Q1 (single-select, 2 options) → 2 checkboxes + 1 free-form.
    # Q2 (multi-select, 3 options)  → 3 checkboxes + 1 free-form.
    assert {
        "q0_opt0", "q0_opt1", "q0_other",
        "q1_opt0", "q1_opt1", "q1_opt2", "q1_other",
    } <= set(props), set(props)
    for key in ("q0_opt0", "q0_opt1", "q1_opt0", "q1_opt1", "q1_opt2"):
        assert props[key]["type"] == "boolean", props[key]
    for key in ("q0_other", "q1_other"):
        assert props[key]["type"] == "string", props[key]
    # Descriptions thread the question text + label so flat checkboxes
    # remain readable.
    assert "Which DB?" in props["q0_opt0"]["description"]
    assert "Postgres" in props["q0_opt0"]["description"]
    assert "Tags?" in props["q1_opt0"]["description"]
    print("OK test_outbound_schema_uses_boolean_per_option_and_freeform")


async def test_inbound_form_submission_reshaped_for_model() -> None:
    """Operator submits the bundled form (boolean keys). The plugin
    reshapes it back to the natural `{status, answers: {question: ...}}`
    shape before the LLM sees the next turn."""
    runner, llm = _build_runner_with_form_plugin(
        llm_responses=[
            _tool_call_response("call-1", "ask_user_question", _multi_question_args()),
            _text_response("got answers"),
        ]
    )
    await _create_session(runner, "alice", "s-form-inbound")

    # Turn 1: ask.
    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-form-inbound",
        new_message=types.Content(role="user", parts=[types.Part(text="ask me")]),
    ):
        pass

    # Operator submits the bundled-UI form widget output. Q1: Postgres
    # (radio tick). Q2: urgent + docs + custom text.
    form_submission = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id="call-1",
                    name="ask_user_question",
                    response={
                        "q0_opt0": True, "q0_opt1": False, "q0_other": None,
                        "q1_opt0": True, "q1_opt1": False, "q1_opt2": True,
                        "q1_other": "experimental",
                    },
                )
            )
        ],
    )

    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-form-inbound",
        new_message=form_submission,
    ):
        pass

    # LLM should have been called once more after the form submission
    # (which means the reshaped response made it through ADK's flow).
    assert llm.calls_made == 2, llm.calls_made

    # Walk session events for the user-side function_response — it
    # should now be the reshaped natural form.
    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="alice", session_id="s-form-inbound"
    )
    fr = None
    for ev in session.events:
        if getattr(ev, "author", None) != "user":
            continue
        for resp in ev.get_function_responses():
            if resp.name == "ask_user_question":
                fr = resp
                break
        if fr:
            break
    assert fr is not None, "no ask_user_question function_response in session"
    assert fr.response == {
        "status": "answered",
        "answers": {
            "Which DB?": "Postgres",
            "Tags?": ["urgent", "docs", "experimental"],
        },
    }, fr.response
    print("OK test_inbound_form_submission_reshaped_for_model")


async def test_inbound_freeform_overrides_radio_tick() -> None:
    """Single-select: operator ticked Postgres AND typed something
    custom. The typed text wins (explicit input shouldn't be silently
    discarded)."""
    runner, _ = _build_runner_with_form_plugin(
        llm_responses=[
            _tool_call_response("call-1", "ask_user_question", _ask_args()),
            _text_response("ok"),
        ]
    )
    await _create_session(runner, "alice", "s-form-freeform")

    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-form-freeform",
        new_message=types.Content(role="user", parts=[types.Part(text="ask me")]),
    ):
        pass

    form_submission = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id="call-1", name="ask_user_question",
                    response={
                        "q0_opt0": True,
                        "q0_opt1": False,
                        "q0_other": "neither, I want SQLite",
                    },
                )
            )
        ],
    )

    async for _ in runner.run_async(
        user_id="alice",
        session_id="s-form-freeform",
        new_message=form_submission,
    ):
        pass

    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="alice", session_id="s-form-freeform"
    )
    fr = next(
        resp
        for ev in session.events
        if getattr(ev, "author", None) == "user"
        for resp in ev.get_function_responses()
        if resp.name == "ask_user_question"
    )
    assert fr.response["answers"]["Which path do you prefer?"] == "neither, I want SQLite"
    print("OK test_inbound_freeform_overrides_radio_tick")


# --- Driver ---------------------------------------------------------


async def main() -> None:
    await test_loop_pauses_after_ask_user_question()
    await test_no_function_response_event_for_initial_call()
    await test_resume_with_user_answer_continues_loop()
    await test_long_running_flag_propagates_to_event()
    # With AskUserQuestionUiHintPlugin wired (PR #15)
    await test_outbound_schema_uses_boolean_per_option_and_freeform()
    await test_inbound_form_submission_reshaped_for_model()
    await test_inbound_freeform_overrides_radio_tick()
    print("\nall e2e ask_user_question tests passed")


if __name__ == "__main__":
    asyncio.run(main())
