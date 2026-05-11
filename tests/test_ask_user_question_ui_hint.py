"""Unit tests for AskUserQuestionUiHintPlugin.

Covers:
  - Single-select question → property with enum on the property itself.
  - multi_select=True → property with type=array and enum on items.
  - Mixed questions (single + multi in one call).
  - Non-`ask_user_question` function calls untouched.
  - Pre-existing `response_schema` on the args not clobbered.
  - Missing/empty questions → no-op (schema not injected).
  - Question without options → string property with no enum (free-form).
  - Schema is JSON-shaped: type=object, required list mirrors property keys.

Run: `.venv/bin/python tests/test_ask_user_question_ui_hint.py`
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adk_cc.plugins.ask_user_question_ui import (
    AskUserQuestionUiHintPlugin,
    _build_response_schema,
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


def _run(plugin: AskUserQuestionUiHintPlugin, resp: LlmResponse) -> Optional[LlmResponse]:
    return asyncio.run(
        plugin.after_model_callback(callback_context=None, llm_response=resp)
    )


def _args_of(resp: LlmResponse) -> dict:
    return dict(resp.content.parts[0].function_call.args or {})


# --- _build_response_schema unit cases ------------------------------


def test_build_schema_single_select() -> None:
    schema = _build_response_schema(
        [
            {
                "question": "Which DB?",
                "header": "DB",
                "options": [
                    {"label": "Postgres", "description": "..."},
                    {"label": "MySQL", "description": "..."},
                ],
                "multi_select": False,
            }
        ]
    )
    assert schema is not None
    assert schema["type"] == "object"
    assert schema["required"] == ["Which DB?"]
    prop = schema["properties"]["Which DB?"]
    assert prop["type"] == "string"
    assert prop["enum"] == ["Postgres", "MySQL"]
    assert prop["description"] == "Which DB?"
    print("OK test_build_schema_single_select")


def test_build_schema_multi_select() -> None:
    schema = _build_response_schema(
        [
            {
                "question": "Pick all that apply",
                "header": "Pick",
                "options": [
                    {"label": "A", "description": "..."},
                    {"label": "B", "description": "..."},
                ],
                "multi_select": True,
            }
        ]
    )
    assert schema is not None
    prop = schema["properties"]["Pick all that apply"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "string", "enum": ["A", "B"]}
    print("OK test_build_schema_multi_select")


def test_build_schema_mixed_questions() -> None:
    schema = _build_response_schema(
        [
            {
                "question": "Q1",
                "header": "Q1",
                "options": [{"label": "x", "description": ""}, {"label": "y", "description": ""}],
                "multi_select": False,
            },
            {
                "question": "Q2",
                "header": "Q2",
                "options": [{"label": "a", "description": ""}, {"label": "b", "description": ""}],
                "multi_select": True,
            },
        ]
    )
    assert schema is not None
    assert set(schema["properties"].keys()) == {"Q1", "Q2"}
    assert schema["properties"]["Q1"]["type"] == "string"
    assert schema["properties"]["Q2"]["type"] == "array"
    assert schema["required"] == ["Q1", "Q2"]
    print("OK test_build_schema_mixed_questions")


def test_build_schema_question_without_options_is_freeform_string() -> None:
    schema = _build_response_schema(
        [{"question": "Free text?", "options": [], "multi_select": False}]
    )
    assert schema is not None
    prop = schema["properties"]["Free text?"]
    assert prop == {"type": "string", "description": "Free text?"}
    print("OK test_build_schema_question_without_options_is_freeform_string")


def test_build_schema_empty_returns_none() -> None:
    assert _build_response_schema([]) is None
    assert _build_response_schema([{"options": []}]) is None  # no `question` key
    assert _build_response_schema([{"question": ""}]) is None  # empty question
    print("OK test_build_schema_empty_returns_none")


# --- Plugin-level after_model_callback ------------------------------


def test_plugin_injects_schema_into_ask_user_question_args() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    questions = [
        {
            "question": "Q1",
            "header": "Q1",
            "options": [
                {"label": "x", "description": ""},
                {"label": "y", "description": ""},
            ],
            "multi_select": False,
        }
    ]
    resp = _llm_response_with_call("ask_user_question", {"questions": questions})
    _run(plugin, resp)

    args = _args_of(resp)
    assert "response_schema" in args
    schema = args["response_schema"]
    assert schema["type"] == "object"
    assert schema["properties"]["Q1"]["enum"] == ["x", "y"]
    print("OK test_plugin_injects_schema_into_ask_user_question_args")


def test_plugin_leaves_other_function_calls_alone() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    resp = _llm_response_with_call("run_bash", {"command": "ls"})
    _run(plugin, resp)
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
                {
                    "question": "Q",
                    "options": [{"label": "a", "description": ""}, {"label": "b", "description": ""}],
                    "multi_select": False,
                }
            ],
            "response_schema": pre_existing,
        },
    )
    _run(plugin, resp)
    assert _args_of(resp)["response_schema"] == pre_existing
    print("OK test_plugin_does_not_clobber_existing_schema")


def test_plugin_no_op_when_questions_missing() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    resp = _llm_response_with_call("ask_user_question", {})  # no questions
    _run(plugin, resp)
    args = _args_of(resp)
    assert "response_schema" not in args
    print("OK test_plugin_no_op_when_questions_missing")


def test_plugin_returns_none_to_keep_original_response() -> None:
    """Mutation is in place; the callback returns None so ADK uses the
    (now-mutated) original response."""
    plugin = AskUserQuestionUiHintPlugin()
    resp = _llm_response_with_call(
        "ask_user_question",
        {
            "questions": [
                {
                    "question": "Q",
                    "options": [{"label": "a", "description": ""}, {"label": "b", "description": ""}],
                    "multi_select": False,
                }
            ]
        },
    )
    out = _run(plugin, resp)
    assert out is None, out
    # But the mutation IS applied to resp.
    assert "response_schema" in _args_of(resp)
    print("OK test_plugin_returns_none_to_keep_original_response")


def test_plugin_handles_response_without_content_gracefully() -> None:
    plugin = AskUserQuestionUiHintPlugin()
    empty = LlmResponse(content=None, partial=False)
    out = _run(plugin, empty)
    assert out is None
    print("OK test_plugin_handles_response_without_content_gracefully")


def main() -> None:
    test_build_schema_single_select()
    test_build_schema_multi_select()
    test_build_schema_mixed_questions()
    test_build_schema_question_without_options_is_freeform_string()
    test_build_schema_empty_returns_none()
    test_plugin_injects_schema_into_ask_user_question_args()
    test_plugin_leaves_other_function_calls_alone()
    test_plugin_does_not_clobber_existing_schema()
    test_plugin_no_op_when_questions_missing()
    test_plugin_returns_none_to_keep_original_response()
    test_plugin_handles_response_without_content_gracefully()
    print("\nall ask_user_question_ui_hint tests passed")


if __name__ == "__main__":
    main()
