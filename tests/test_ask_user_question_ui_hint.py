"""Unit tests for AskUserQuestionUiHintPlugin.

Outbound (`after_model_callback`):
  - Single-select question → boolean property per option (so the
    bundled UI renders a checkbox per option) + one optional
    free-form `q{i}_other` string per question.
  - multi_select=True → same boolean-per-option shape; description
    tells the operator to tick any that apply.
  - Mixed (single + multi) → properties keyed by position so the
    inbound rewrite can disambig.
  - Non-`ask_user_question` function calls untouched.
  - Pre-existing `response_schema` not clobbered.
  - Missing/empty questions → no-op.
  - Question without options → just the free-form string; preserves
    the "ask anything" path.

Inbound (`on_user_message_callback`):
  - Form-widget response (`{q0_opt0: true, q0_other: "...", ...}`) →
    reshaped to `{status: "answered", answers: {<qtext>: <answer>}}`
    matching the tool's documented output shape.
  - multi_select=True → answer is a list of every ticked option
    label, with any free-form text appended.
  - multi_select=False + nothing typed → first ticked label wins.
  - multi_select=False + free-form text + tick → free-form wins.
  - No tick + no text → empty-string answer.
  - Natural-shape response (custom frontend submitted
    `{question: answer}` directly) passes through unchanged.
  - Function-call not in session events → no reshape (defensive).

Run: `.venv/bin/python tests/test_ask_user_question_ui_hint.py`
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from google.adk.events.event import Event
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adk_cc.plugins.ask_user_question_ui import (
    ASK_USER_QUESTION_TOOL_NAME,
    AskUserQuestionUiHintPlugin,
    _build_response_schema,
    _looks_like_form_widget_response,
    _reshape_answers,
)


# --- Fixtures -------------------------------------------------------


def _llm_response_with_call(name: str, args: dict, call_id: str = "c1") -> LlmResponse:
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


def _run_after_model(
    plugin: AskUserQuestionUiHintPlugin, resp: LlmResponse
) -> Optional[LlmResponse]:
    return asyncio.run(
        plugin.after_model_callback(callback_context=None, llm_response=resp)
    )


def _args_of(resp: LlmResponse) -> dict:
    return dict(resp.content.parts[0].function_call.args or {})


class _FakeSession:
    def __init__(self, events: Optional[list] = None) -> None:
        self.events = list(events or [])


class _FakeInvocationCtx:
    def __init__(self, events: Optional[list] = None) -> None:
        self.session = _FakeSession(events)


def _ask_call_event(call_id: str, questions: list) -> Event:
    return Event(
        invocation_id="inv-1",
        author="test-agent",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id=call_id,
                        name=ASK_USER_QUESTION_TOOL_NAME,
                        args={"questions": questions},
                    )
                )
            ],
        ),
    )


def _user_response(call_id: str, response: dict) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=call_id,
                    name=ASK_USER_QUESTION_TOOL_NAME,
                    response=response,
                )
            )
        ],
    )


def _run_user_msg(
    plugin: AskUserQuestionUiHintPlugin,
    msg: types.Content,
    *,
    invocation_context: Optional[Any] = None,
) -> Optional[types.Content]:
    return asyncio.run(
        plugin.on_user_message_callback(
            invocation_context=invocation_context, user_message=msg
        )
    )


# --- Outbound: _build_response_schema unit cases -------------------


def test_build_schema_single_select_renders_checkboxes() -> None:
    schema = _build_response_schema(
        [
            {
                "question": "Which DB?",
                "header": "DB",
                "options": [
                    {"label": "Postgres", "description": "Strong tooling"},
                    {"label": "MySQL", "description": "Wide support"},
                ],
                "multi_select": False,
            }
        ]
    )
    assert schema is not None
    assert schema["type"] == "object"
    props = schema["properties"]
    # Two checkbox properties + one free-form text per question.
    assert set(props.keys()) == {"q0_opt0", "q0_opt1", "q0_other"}
    assert props["q0_opt0"]["type"] == "boolean"
    assert props["q0_opt1"]["type"] == "boolean"
    assert props["q0_other"]["type"] == "string"
    # Description carries question + label + option description so the
    # bundled UI's flat checkbox list reads correctly.
    assert "Which DB?" in props["q0_opt0"]["description"]
    assert "Postgres" in props["q0_opt0"]["description"]
    assert "Strong tooling" in props["q0_opt0"]["description"]
    # Pick hint reflects single-select.
    assert "only one" in props["q0_opt0"]["description"]
    print("OK test_build_schema_single_select_renders_checkboxes")


def test_build_schema_multi_select_pick_any_hint() -> None:
    schema = _build_response_schema(
        [
            {
                "question": "Tags?",
                "options": [
                    {"label": "urgent"},
                    {"label": "cleanup"},
                    {"label": "docs"},
                ],
                "multi_select": True,
            }
        ]
    )
    assert schema is not None
    props = schema["properties"]
    assert set(props.keys()) == {"q0_opt0", "q0_opt1", "q0_opt2", "q0_other"}
    for opt_key in ("q0_opt0", "q0_opt1", "q0_opt2"):
        assert props[opt_key]["type"] == "boolean"
        assert "any that apply" in props[opt_key]["description"]
    print("OK test_build_schema_multi_select_pick_any_hint")


def test_build_schema_mixed_questions_positional_keys() -> None:
    schema = _build_response_schema(
        [
            {
                "question": "Q1",
                "options": [{"label": "x"}, {"label": "y"}],
                "multi_select": False,
            },
            {
                "question": "Q2",
                "options": [{"label": "a"}, {"label": "b"}],
                "multi_select": True,
            },
        ]
    )
    assert schema is not None
    props = schema["properties"]
    # Each question gets its own positional namespace.
    assert {"q0_opt0", "q0_opt1", "q0_other", "q1_opt0", "q1_opt1", "q1_other"} <= set(props)
    assert "only one" in props["q0_opt0"]["description"]
    assert "any that apply" in props["q1_opt0"]["description"]
    print("OK test_build_schema_mixed_questions_positional_keys")


def test_build_schema_question_without_options_just_freeform() -> None:
    """Question with no options still gets a free-form text field —
    preserves the 'ask anything' path."""
    schema = _build_response_schema(
        [{"question": "Free text?", "options": [], "multi_select": False}]
    )
    assert schema is not None
    props = schema["properties"]
    assert set(props.keys()) == {"q0_other"}
    assert props["q0_other"]["type"] == "string"
    print("OK test_build_schema_question_without_options_just_freeform")


def test_build_schema_empty_returns_none() -> None:
    assert _build_response_schema([]) is None
    assert _build_response_schema([{"options": []}]) is None  # no `question`
    assert _build_response_schema([{"question": ""}]) is None  # empty question
    print("OK test_build_schema_empty_returns_none")


# --- Outbound: plugin-level after_model_callback ------------------


def test_plugin_injects_schema_into_ask_user_question_args() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    questions = [
        {
            "question": "Q1",
            "options": [{"label": "x"}, {"label": "y"}],
            "multi_select": False,
        }
    ]
    resp = _llm_response_with_call("ask_user_question", {"questions": questions})
    _run_after_model(plugin, resp)

    args = _args_of(resp)
    assert "response_schema" in args
    schema = args["response_schema"]
    assert schema["type"] == "object"
    assert {"q0_opt0", "q0_opt1", "q0_other"} <= set(schema["properties"])
    print("OK test_plugin_injects_schema_into_ask_user_question_args")


def test_plugin_leaves_other_function_calls_alone() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    resp = _llm_response_with_call("run_bash", {"command": "ls"})
    _run_after_model(plugin, resp)
    args = _args_of(resp)
    assert "response_schema" not in args
    assert args == {"command": "ls"}
    print("OK test_plugin_leaves_other_function_calls_alone")


def test_plugin_does_not_clobber_existing_schema() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    pre_existing = {"type": "object", "properties": {"already": {"type": "string"}}}
    resp = _llm_response_with_call(
        "ask_user_question",
        {
            "questions": [
                {"question": "Q", "options": [{"label": "a"}], "multi_select": False}
            ],
            "response_schema": pre_existing,
        },
    )
    _run_after_model(plugin, resp)
    assert _args_of(resp)["response_schema"] == pre_existing
    print("OK test_plugin_does_not_clobber_existing_schema")


def test_plugin_no_op_when_questions_missing() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    resp = _llm_response_with_call("ask_user_question", {})
    _run_after_model(plugin, resp)
    assert "response_schema" not in _args_of(resp)
    print("OK test_plugin_no_op_when_questions_missing")


def test_plugin_handles_response_without_content_gracefully() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    empty = LlmResponse(content=None, partial=False)
    out = _run_after_model(plugin, empty)
    assert out is None
    print("OK test_plugin_handles_response_without_content_gracefully")


# --- Inbound: _reshape_answers unit cases --------------------------


def test_reshape_single_select_first_tick_wins() -> None:
    qs = [
        {
            "question": "Path?",
            "options": [{"label": "Refactor"}, {"label": "Patch"}],
            "multi_select": False,
        }
    ]
    out = _reshape_answers(
        {"q0_opt0": True, "q0_opt1": False, "q0_other": None}, qs
    )
    assert out == {"Path?": "Refactor"}
    print("OK test_reshape_single_select_first_tick_wins")


def test_reshape_single_select_freeform_overrides_tick() -> None:
    """Operator ticked 'Refactor' AND typed 'something else' — typed
    text wins so explicit input isn't silently discarded."""
    qs = [
        {
            "question": "Path?",
            "options": [{"label": "Refactor"}, {"label": "Patch"}],
            "multi_select": False,
        }
    ]
    out = _reshape_answers(
        {"q0_opt0": True, "q0_opt1": False, "q0_other": "something else"}, qs
    )
    assert out == {"Path?": "something else"}
    print("OK test_reshape_single_select_freeform_overrides_tick")


def test_reshape_multi_select_includes_all_ticks_and_freeform() -> None:
    qs = [
        {
            "question": "Tags?",
            "options": [{"label": "urgent"}, {"label": "cleanup"}, {"label": "docs"}],
            "multi_select": True,
        }
    ]
    out = _reshape_answers(
        {
            "q0_opt0": True,
            "q0_opt1": False,
            "q0_opt2": True,
            "q0_other": "experimental",
        },
        qs,
    )
    assert out == {"Tags?": ["urgent", "docs", "experimental"]}
    print("OK test_reshape_multi_select_includes_all_ticks_and_freeform")


def test_reshape_multi_select_no_freeform() -> None:
    qs = [
        {
            "question": "Tags?",
            "options": [{"label": "a"}, {"label": "b"}],
            "multi_select": True,
        }
    ]
    out = _reshape_answers(
        {"q0_opt0": True, "q0_opt1": True, "q0_other": ""}, qs
    )
    assert out == {"Tags?": ["a", "b"]}
    print("OK test_reshape_multi_select_no_freeform")


def test_reshape_no_tick_no_text_yields_empty_string() -> None:
    qs = [
        {
            "question": "Path?",
            "options": [{"label": "Refactor"}, {"label": "Patch"}],
            "multi_select": False,
        }
    ]
    out = _reshape_answers(
        {"q0_opt0": False, "q0_opt1": False, "q0_other": None}, qs
    )
    assert out == {"Path?": ""}
    print("OK test_reshape_no_tick_no_text_yields_empty_string")


def test_reshape_handles_missing_keys_defensively() -> None:
    """If the bundled UI omits some keys (rare; defensive), treat as
    untouched (false / no text)."""
    qs = [
        {
            "question": "Path?",
            "options": [{"label": "Refactor"}],
            "multi_select": False,
        }
    ]
    out = _reshape_answers({}, qs)
    assert out == {"Path?": ""}
    print("OK test_reshape_handles_missing_keys_defensively")


def test_reshape_handles_multi_question() -> None:
    qs = [
        {"question": "Q1", "options": [{"label": "x"}], "multi_select": False},
        {"question": "Q2", "options": [{"label": "a"}, {"label": "b"}], "multi_select": True},
    ]
    out = _reshape_answers(
        {
            "q0_opt0": True,
            "q0_other": "",
            "q1_opt0": True,
            "q1_opt1": True,
            "q1_other": None,
        },
        qs,
    )
    assert out == {"Q1": "x", "Q2": ["a", "b"]}
    print("OK test_reshape_handles_multi_question")


# --- Inbound: _looks_like_form_widget_response --------------------


def test_looks_like_form_widget_response_positive() -> None:
    assert _looks_like_form_widget_response(
        {"q0_opt0": True, "q0_other": ""}
    ) is True
    assert _looks_like_form_widget_response({"q0_opt5": False}) is True
    assert _looks_like_form_widget_response({"q3_other": "text"}) is True
    print("OK test_looks_like_form_widget_response_positive")


def test_looks_like_form_widget_response_negative() -> None:
    """A natural-shape `{question: answer}` should NOT be treated as
    form-widget output — custom frontends should pass through."""
    assert _looks_like_form_widget_response(
        {"Path?": "Refactor"}
    ) is False
    assert _looks_like_form_widget_response(
        {"status": "answered", "answers": {"Q": "A"}}
    ) is False
    assert _looks_like_form_widget_response({}) is False
    print("OK test_looks_like_form_widget_response_negative")


# --- Inbound: plugin-level on_user_message_callback ---------------


def test_inbound_reshape_single_select_via_plugin() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    qs = [
        {
            "question": "Path?",
            "options": [{"label": "Refactor"}, {"label": "Patch"}],
            "multi_select": False,
        }
    ]
    ctx = _FakeInvocationCtx([_ask_call_event("c1", qs)])
    msg = _user_response(
        "c1",
        {"q0_opt0": True, "q0_opt1": False, "q0_other": None},
    )
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    assert out is not None
    fr = out.parts[0].function_response
    assert fr.name == ASK_USER_QUESTION_TOOL_NAME
    assert fr.response == {
        "status": "answered",
        "answers": {"Path?": "Refactor"},
    }
    print("OK test_inbound_reshape_single_select_via_plugin")


def test_inbound_reshape_multi_select_via_plugin() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    qs = [
        {
            "question": "Tags?",
            "options": [{"label": "a"}, {"label": "b"}, {"label": "c"}],
            "multi_select": True,
        }
    ]
    ctx = _FakeInvocationCtx([_ask_call_event("c1", qs)])
    msg = _user_response(
        "c1",
        {"q0_opt0": True, "q0_opt1": False, "q0_opt2": True, "q0_other": "x"},
    )
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    fr = out.parts[0].function_response
    assert fr.response == {
        "status": "answered",
        "answers": {"Tags?": ["a", "c", "x"]},
    }
    print("OK test_inbound_reshape_multi_select_via_plugin")


def test_inbound_natural_shape_passes_through() -> None:
    """Custom payload-aware frontends can submit the natural shape
    directly and skip the reshape."""
    plugin = AskUserQuestionUiHintPlugin()
    qs = [{"question": "Q", "options": [{"label": "a"}], "multi_select": False}]
    ctx = _FakeInvocationCtx([_ask_call_event("c1", qs)])
    natural = {"status": "answered", "answers": {"Q": "a"}}
    msg = _user_response("c1", natural)
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    # No reshape needed → returns None, original message goes through.
    assert out is None
    assert msg.parts[0].function_response.response == natural
    print("OK test_inbound_natural_shape_passes_through")


def test_inbound_no_matching_call_in_session_leaves_alone() -> None:
    """Defensive: response references a call_id we can't find in
    session. Skip the reshape rather than guessing."""
    plugin = AskUserQuestionUiHintPlugin()
    ctx = _FakeInvocationCtx([])
    msg = _user_response("unknown-id", {"q0_opt0": True, "q0_other": None})
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    assert out is None
    print("OK test_inbound_no_matching_call_in_session_leaves_alone")


def test_inbound_ignores_other_function_responses() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    msg = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id="t1", name="run_bash", response={"status": "ok"}
                )
            )
        ],
    )
    out = _run_user_msg(plugin, msg)
    assert out is None
    print("OK test_inbound_ignores_other_function_responses")


def test_inbound_handles_empty_parts() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    empty = types.Content(role="user", parts=[])
    out = _run_user_msg(plugin, empty)
    assert out is None
    print("OK test_inbound_handles_empty_parts")


# --- Driver --------------------------------------------------------


def main() -> None:
    test_build_schema_single_select_renders_checkboxes()
    test_build_schema_multi_select_pick_any_hint()
    test_build_schema_mixed_questions_positional_keys()
    test_build_schema_question_without_options_just_freeform()
    test_build_schema_empty_returns_none()
    test_plugin_injects_schema_into_ask_user_question_args()
    test_plugin_leaves_other_function_calls_alone()
    test_plugin_does_not_clobber_existing_schema()
    test_plugin_no_op_when_questions_missing()
    test_plugin_handles_response_without_content_gracefully()
    test_reshape_single_select_first_tick_wins()
    test_reshape_single_select_freeform_overrides_tick()
    test_reshape_multi_select_includes_all_ticks_and_freeform()
    test_reshape_multi_select_no_freeform()
    test_reshape_no_tick_no_text_yields_empty_string()
    test_reshape_handles_missing_keys_defensively()
    test_reshape_handles_multi_question()
    test_looks_like_form_widget_response_positive()
    test_looks_like_form_widget_response_negative()
    test_inbound_reshape_single_select_via_plugin()
    test_inbound_reshape_multi_select_via_plugin()
    test_inbound_natural_shape_passes_through()
    test_inbound_no_matching_call_in_session_leaves_alone()
    test_inbound_ignores_other_function_responses()
    test_inbound_handles_empty_parts()
    print("\nall ask_user_question_ui_hint tests passed")


if __name__ == "__main__":
    main()
