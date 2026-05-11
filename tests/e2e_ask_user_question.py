"""End-to-end test that `ask_user_question` actually pauses the agent loop.

Bug being verified: without the `skip_summarization` invariant for
long-running tools in `AdkCcTool.run_async`, ADK's function-response
event for `ask_user_question` isn't marked final
(`long_running_tool_ids` is set on the function-CALL event but not the
RESPONSE event built by `__build_response_event`). The runner therefore
re-invokes the LLM with `{"status": "awaiting_user_input"}` as a normal
tool result and the model cascades into more questions — same root
cause as the confirmation-cascade bug we fixed earlier.

Test strategy: queue exactly ONE LLM response (the initial
`function_call`) on the scripted LLM. If the loop pauses correctly the
queue is never re-consumed. If it cascades, `_ScriptedLlm` raises
"queue empty" and the test fails loudly.

Run: `.venv/bin/python tests/e2e_ask_user_question.py`
"""

from __future__ import annotations

import asyncio
import json
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

    # Verify the function_response carries the awaiting_user_input payload.
    awaiting_seen = False
    for ev in events:
        for fr in ev.get_function_responses():
            resp = fr.response
            if isinstance(resp, dict) and resp.get("status") == "awaiting_user_input":
                qs = resp.get("questions") or []
                assert len(qs) == 1 and qs[0]["header"] == "Path", qs
                awaiting_seen = True
    assert awaiting_seen, "expected awaiting_user_input function_response in events"

    # The function-response event itself must be final
    # (is_final_response() True), otherwise ADK's loop would have iterated.
    last_response_event = next(
        (e for e in reversed(events) if e.get_function_responses()), None
    )
    assert last_response_event is not None
    assert last_response_event.is_final_response(), (
        "function-response event must be is_final_response() for the loop to pause; "
        "either skip_summarization or long_running_tool_ids must be set"
    )
    assert last_response_event.actions.skip_summarization is True, (
        "AdkCcTool.run_async should set skip_summarization for long_running tools"
    )
    print("OK test_loop_pauses_after_ask_user_question")


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

    # Invocation 2: user submits the answer as a function_response with
    # the same call_id. The session history then contains both the
    # awaiting_user_input result and the answered result; the LLM sees both.
    answer = {
        "status": "answered",
        "answers": {"Which path do you prefer?": "Refactor"},
    }
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
    has the tool's call_id in `long_running_tool_ids`. This is the OTHER
    half of the pause mechanism (the half ADK does for us)."""
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
    # At least one function-call event must list call-1 in long_running_tool_ids.
    flagged = any(
        e.long_running_tool_ids and "call-1" in e.long_running_tool_ids
        for e in call_events
    )
    assert flagged, (
        "ADK should mark the function-CALL event with long_running_tool_ids "
        "for tools whose is_long_running=True"
    )
    print("OK test_long_running_flag_propagates_to_event")


# --- Driver ---------------------------------------------------------


async def main() -> None:
    await test_loop_pauses_after_ask_user_question()
    await test_resume_with_user_answer_continues_loop()
    await test_long_running_flag_propagates_to_event()
    print("\nall e2e ask_user_question tests passed")


if __name__ == "__main__":
    asyncio.run(main())
