"""Unit tests for `adk_cc/permissions/token_counter.py`.

The shared estimator mirrors ADK's algorithm at
`apps/compaction.py:91-139` so `ContextGuardPlugin` and
`EventsCompactionConfig` agree on prompt token counts. Tests cover:

  - usage_metadata precedence: latest event's prompt_token_count wins
    over the chars/4 fallback (matches ADK's `_latest_prompt_token_count`).
  - Multiple events with usage_metadata: the MOST RECENT (by list
    order) wins, not the first.
  - usage_metadata=None / missing: falls through to chars/4.
  - chars/4 fallback over llm_request.contents matches ADK's
    `_count_text_chars_in_content` exactly for the same input.
  - Empty / None / missing-parts content: returns 0 cleanly.
  - Multi-part contents: only text parts count (function_call,
    function_response, inline_data are zero — matches ADK).
  - Per-content char count agrees with ADK's helper.

Run: `.venv/bin/python tests/test_token_counter.py`
"""

from __future__ import annotations

import os
from typing import Any, Optional

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.permissions.token_counter import (
    _count_text_chars_in_content,
    _estimate_from_request,
    estimate_prompt_tokens,
)


# --- Fakes ---------------------------------------------------------


class _FakePart:
    def __init__(
        self,
        text: Optional[str] = None,
        function_call: Any = None,
        function_response: Any = None,
    ) -> None:
        self.text = text
        self.function_call = function_call
        self.function_response = function_response


class _FakeContent:
    def __init__(self, parts: Optional[list] = None) -> None:
        self.parts = parts or []


class _FakeRequest:
    def __init__(self, contents: Optional[list] = None) -> None:
        self.contents = contents or []


class _FakeUsage:
    def __init__(self, prompt_token_count: Optional[int]) -> None:
        self.prompt_token_count = prompt_token_count


class _FakeEvent:
    def __init__(self, prompt_token_count: Optional[int]) -> None:
        if prompt_token_count is None:
            self.usage_metadata = None
        else:
            self.usage_metadata = _FakeUsage(prompt_token_count)


# --- usage_metadata precedence -------------------------------------


def test_usage_metadata_wins_over_chars_div_4() -> None:
    """When session events carry usage_metadata.prompt_token_count, the
    estimator returns that — even if the chars/4 estimate over the
    request would yield something completely different."""
    req = _FakeRequest(
        contents=[_FakeContent([_FakePart(text="x" * 4000)])]
    )  # chars/4 would yield 1000
    events = [_FakeEvent(prompt_token_count=42)]
    assert estimate_prompt_tokens(req, session_events=events) == 42
    print("OK test_usage_metadata_wins_over_chars_div_4")


def test_most_recent_usage_metadata_wins() -> None:
    """Multiple events with usage_metadata — the MOST RECENT (by list
    order) wins. Mirrors ADK's `reversed(events)` lookup."""
    events = [
        _FakeEvent(prompt_token_count=100),
        _FakeEvent(prompt_token_count=200),
        _FakeEvent(prompt_token_count=300),  # most recent
    ]
    req = _FakeRequest()
    assert estimate_prompt_tokens(req, session_events=events) == 300
    print("OK test_most_recent_usage_metadata_wins")


def test_events_without_metadata_skipped() -> None:
    """An event with usage_metadata=None or missing prompt_token_count
    is skipped — the lookup continues backwards toward older events."""
    events = [
        _FakeEvent(prompt_token_count=500),
        _FakeEvent(prompt_token_count=None),  # newer event, no metadata
    ]
    # The newer event has no metadata, so older wins.
    req = _FakeRequest()
    assert estimate_prompt_tokens(req, session_events=events) == 500
    print("OK test_events_without_metadata_skipped")


def test_no_session_events_falls_back_to_chars_div_4() -> None:
    """`session_events=None` → falls straight to the chars/4
    estimator over `llm_request.contents`."""
    req = _FakeRequest(
        contents=[
            _FakeContent([_FakePart(text="hello world")]),  # 11 chars
            _FakeContent([_FakePart(text="foo bar")]),       # 7 chars
        ]
    )
    # Total 18 chars → 18 // 4 = 4.
    assert estimate_prompt_tokens(req, session_events=None) == 4
    print("OK test_no_session_events_falls_back_to_chars_div_4")


def test_empty_session_events_falls_back_to_chars_div_4() -> None:
    """Empty event list (not None) — same as None, falls through to
    the chars/4 path."""
    req = _FakeRequest(
        contents=[_FakeContent([_FakePart(text="x" * 40)])]
    )
    assert estimate_prompt_tokens(req, session_events=[]) == 10
    print("OK test_empty_session_events_falls_back_to_chars_div_4")


# --- chars/4 fallback ----------------------------------------------


def test_chars_div_4_basic() -> None:
    """`_estimate_from_request` sums text-part chars and divides by 4."""
    req = _FakeRequest(
        contents=[_FakeContent([_FakePart(text="x" * 1000)])]
    )
    assert _estimate_from_request(req) == 250
    print("OK test_chars_div_4_basic")


def test_chars_div_4_empty_contents_returns_zero() -> None:
    """Empty / None contents return 0, not None or an error."""
    assert _estimate_from_request(_FakeRequest()) == 0
    assert _estimate_from_request(_FakeRequest(contents=[])) == 0
    assert _estimate_from_request(None) == 0
    print("OK test_chars_div_4_empty_contents_returns_zero")


def test_chars_div_4_only_text_parts_count() -> None:
    """function_call / function_response / inline_data parts contribute
    ZERO to the chars total — matches ADK's `_count_text_chars_in_content`
    which only sums `part.text`. This is a known under-count for
    tool-heavy turns, but it's the same under-count ADK applies, so
    the two layers stay aligned."""
    req = _FakeRequest(
        contents=[
            _FakeContent([
                _FakePart(text="text part — 16 chars"),       # 20 chars
                _FakePart(function_call={"name": "run_bash"}), # ignored
                _FakePart(function_response={"result": "ok"}), # ignored
            ])
        ]
    )
    # Only the 20 chars from text count → 20 // 4 = 5.
    assert _estimate_from_request(req) == 5
    print("OK test_chars_div_4_only_text_parts_count")


def test_chars_div_4_multi_content() -> None:
    """Multiple contents sum cleanly."""
    req = _FakeRequest(
        contents=[
            _FakeContent([_FakePart(text="x" * 100)]),
            _FakeContent([_FakePart(text="y" * 200)]),
            _FakeContent([_FakePart(text="z" * 300)]),
        ]
    )
    # 600 chars → 150 tokens.
    assert _estimate_from_request(req) == 150
    print("OK test_chars_div_4_multi_content")


def test_chars_div_4_missing_parts() -> None:
    """Content with `parts=None` or empty `parts=[]` contributes 0."""
    req = _FakeRequest(
        contents=[
            _FakeContent(parts=None),
            _FakeContent(parts=[]),
            _FakeContent([_FakePart(text="hi")]),  # 2 chars
        ]
    )
    assert _estimate_from_request(req) == 0  # 2 // 4 = 0
    print("OK test_chars_div_4_missing_parts")


# --- Helper unit ---------------------------------------------------


def test_count_text_chars_in_content_helper() -> None:
    """`_count_text_chars_in_content` mirrors ADK's
    `apps/compaction._count_text_chars_in_content`. None-safe, empty-safe."""
    assert _count_text_chars_in_content(None) == 0
    assert _count_text_chars_in_content(_FakeContent(parts=None)) == 0
    assert _count_text_chars_in_content(_FakeContent(parts=[])) == 0
    assert (
        _count_text_chars_in_content(_FakeContent([_FakePart(text="hello")]))
        == 5
    )
    # Multi-part: only text parts contribute.
    c = _FakeContent([
        _FakePart(text="abc"),
        _FakePart(text=None),  # text=None → 0
        _FakePart(text="defg"),
    ])
    assert _count_text_chars_in_content(c) == 7
    print("OK test_count_text_chars_in_content_helper")


def test_byte_for_byte_with_adk_per_content() -> None:
    """Per-content count agrees with ADK byte-for-byte on a range of
    inputs (empty, ascii, unicode, multiline, long-repeated)."""
    from google.adk.apps.compaction import _count_text_chars_in_content as adk
    from google.genai import types

    cases = [
        "",
        "hello",
        "multi\nline\ntext",
        "x" * 10_000,
        "unicode: ñ é ü 中文 emoji 🚀",
        "tab\tseparated\tcols",
    ]
    for txt in cases:
        c = types.Content(role="user", parts=[types.Part(text=txt)])
        assert _count_text_chars_in_content(c) == adk(c), (
            f"divergence on {txt!r}: ours={_count_text_chars_in_content(c)} adk={adk(c)}"
        )
    print("OK test_byte_for_byte_with_adk_per_content")


# --- Driver --------------------------------------------------------


def main() -> None:
    test_usage_metadata_wins_over_chars_div_4()
    test_most_recent_usage_metadata_wins()
    test_events_without_metadata_skipped()
    test_no_session_events_falls_back_to_chars_div_4()
    test_empty_session_events_falls_back_to_chars_div_4()
    test_chars_div_4_basic()
    test_chars_div_4_empty_contents_returns_zero()
    test_chars_div_4_only_text_parts_count()
    test_chars_div_4_multi_content()
    test_chars_div_4_missing_parts()
    test_count_text_chars_in_content_helper()
    test_byte_for_byte_with_adk_per_content()
    print("\nall token-counter tests passed")


if __name__ == "__main__":
    main()
