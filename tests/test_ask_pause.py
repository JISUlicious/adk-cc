"""Tests for AskPausePlugin — make `ask_user_question` reliably pause the turn.

The plugin's `after_model_callback` drops sibling function-call parts from a
model turn whenever `ask_user_question` is present, so the ask becomes the sole
(long-running) call and the ADK loop pauses for the user's answer. These tests
pin the drop behavior across the turn shapes the model actually emits.

Run: `.venv/bin/python tests/test_ask_pause.py`
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from google.adk.models.llm_response import LlmResponse
from google.genai import types

from adk_cc.plugins.ask_pause import AskPausePlugin

_ASK = "ask_user_question"


def _fc_part(name: str) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args={}))


def _text_part(text: str) -> types.Part:
    return types.Part(text=text)


def _resp(parts: list[types.Part]) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=parts))


def _run(resp: LlmResponse):
    plugin = AskPausePlugin()
    return asyncio.run(
        plugin.after_model_callback(callback_context=None, llm_response=resp)
    )


def _call_names(resp: LlmResponse) -> list[str]:
    return [
        p.function_call.name
        for p in resp.content.parts
        if getattr(p, "function_call", None) is not None
    ]


def test_drops_sibling_when_ask_present() -> None:
    resp = _resp([_fc_part("run_bash"), _fc_part(_ASK)])
    out = _run(resp)
    assert out is None  # in-place mutation, returns None
    assert _call_names(resp) == [_ASK], "run_bash sibling should be dropped"
    print("OK test_drops_sibling_when_ask_present")


def test_drops_multiple_siblings() -> None:
    resp = _resp([_fc_part("run_bash"), _fc_part(_ASK), _fc_part("glob_files")])
    _run(resp)
    assert _call_names(resp) == [_ASK], "all non-ask calls dropped"
    print("OK test_drops_multiple_siblings")


def test_keeps_text_and_thought_parts() -> None:
    resp = _resp([_text_part("thinking..."), _fc_part("run_bash"), _fc_part(_ASK)])
    _run(resp)
    parts = resp.content.parts
    assert any(getattr(p, "text", None) == "thinking..." for p in parts), "text kept"
    assert _call_names(resp) == [_ASK], "only the ask call survives"
    print("OK test_keeps_text_and_thought_parts")


def test_ask_alone_untouched() -> None:
    resp = _resp([_fc_part(_ASK)])
    out = _run(resp)
    assert out is None
    assert _call_names(resp) == [_ASK]
    print("OK test_ask_alone_untouched")


def test_no_ask_untouched() -> None:
    resp = _resp([_fc_part("run_bash"), _fc_part("glob_files")])
    _run(resp)
    assert _call_names(resp) == ["run_bash", "glob_files"], "no ask → nothing dropped"
    print("OK test_no_ask_untouched")


def test_empty_and_textonly_untouched() -> None:
    # No parts at all.
    empty = LlmResponse(content=types.Content(role="model", parts=[]))
    assert _run(empty) is None
    # Text only, no function calls.
    textonly = _resp([_text_part("hello")])
    assert _run(textonly) is None
    assert _call_names(textonly) == []
    print("OK test_empty_and_textonly_untouched")


def main() -> None:
    test_drops_sibling_when_ask_present()
    test_drops_multiple_siblings()
    test_keeps_text_and_thought_parts()
    test_ask_alone_untouched()
    test_no_ask_untouched()
    test_empty_and_textonly_untouched()
    print("\nall ask-pause tests passed")


if __name__ == "__main__":
    main()
