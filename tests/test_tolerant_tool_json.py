"""Tests for tolerant tool-call JSON parsing (plugins/tolerant_tool_json).

Models sometimes emit tool-call `function.arguments` that aren't strictly
valid JSON — invalid backslash escapes, raw control chars in string values,
trailing commas. ADK's bare json.loads then crashes the whole turn
("Invalid \\escape"). The patch makes ADK's lite_llm tool-arg parse tolerant,
recovering these while leaving valid JSON byte-identical and still failing on
genuinely-broken input.

Hand-rolled (no pytest).
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.plugins.tolerant_tool_json import (
    tolerant_loads,
    install_tolerant_tool_json,
    _TolerantJsonShim,
    TRUNCATED_TOOL_CALL_KEY,
)


# --- recovers the common malformations -----------------------------------

def test_invalid_backslash_escape():
    # <script>x=\d+</script> — the literal \d the model didn't escape.
    out = tolerant_loads('{"content": "<script>x=\\\\d+</script>"}'.replace("\\\\", "\\"))
    assert out["content"] == "<script>x=\\d+</script>", out
    print("OK test_invalid_backslash_escape")


def test_raw_control_chars_in_string():
    # un-escaped newline + tab inside a string value (common with HTML/code).
    out = tolerant_loads('{"content": "line1\nline2\tend"}')
    assert out["content"] == "line1\nline2\tend", out
    print("OK test_raw_control_chars_in_string")


def test_trailing_comma_object_and_array():
    assert tolerant_loads('{"a": 1,}') == {"a": 1}
    assert tolerant_loads('{"x": [1, 2, 3,]}') == {"x": [1, 2, 3]}
    print("OK test_trailing_comma_object_and_array")


def test_invalid_escape_and_windows_path():
    # \s is NOT a valid JSON escape (unlike \f/\n which ARE) → repaired to a
    # literal backslash-s.
    assert tolerant_loads('{"re": "\\s+"}')["re"] == "\\s+"
    # windows path: \U and \m are invalid escapes → doubled, read back literal
    assert tolerant_loads('{"p": "C:\\Users\\me"}')["p"] == "C:\\Users\\me"
    print("OK test_invalid_escape_and_windows_path")


def test_combined_escape_and_newline():
    out = tolerant_loads('{"c": "x=\\d+\nmore"}')
    assert out["c"] == "x=\\d+\nmore", out
    print("OK test_combined_escape_and_newline")


# --- does NOT corrupt valid JSON -----------------------------------------

def test_valid_json_byte_identical():
    v = json.dumps({
        "path": "a/b.txt",
        "content": 'line1\nline2\ttab "quote" \\ end',
        "re": "\\uABCD",
        "n": 42,
        "arr": [1, 2, {"k": "v"}],
    })
    assert tolerant_loads(v) == json.loads(v), "valid JSON altered!"
    print("OK test_valid_json_byte_identical")


def test_empty_and_simple():
    assert tolerant_loads("{}") == {}
    assert tolerant_loads('"just a string"') == "just a string"
    assert tolerant_loads("123") == 123
    print("OK test_empty_and_simple")


# --- completes a TRUNCATED tool call (missing closers only) --------------
# A model that stops mid-emission leaves structurally-incomplete JSON. When the
# values present are COMPLETE and only the closing }/] is missing, recover it
# losslessly; when a value itself is cut, refuse (don't fabricate).

def test_truncation_missing_brace_recovered():
    # the reported pattern: value complete, only the } is missing
    assert tolerant_loads('{"skill_name": "my-skill"') == {"skill_name": "my-skill"}
    assert tolerant_loads('{"a": 1') == {"a": 1}
    assert tolerant_loads('[1, 2') == [1, 2]
    print("OK test_truncation_missing_brace_recovered")


def test_truncation_lone_open_brace_becomes_empty():
    assert tolerant_loads("{") == {}
    assert tolerant_loads("[") == []
    print("OK test_truncation_lone_open_brace_becomes_empty")


def test_truncation_nested_and_composes_with_repairs():
    assert tolerant_loads('[{"a": 1}') == [{"a": 1}]
    assert tolerant_loads('{"a": [1, 2') == {"a": [1, 2]}
    assert tolerant_loads('{"a": {"b": 1}') == {"a": {"b": 1}}
    assert tolerant_loads('{"a": 1,') == {"a": 1}  # truncated AND trailing comma
    print("OK test_truncation_nested_and_composes_with_repairs")


def test_completion_ignores_brackets_inside_strings():
    # the { inside the string value must NOT be counted as an open bracket
    assert tolerant_loads('{"tmpl": "a {b} c"') == {"tmpl": "a {b} c"}
    print("OK test_completion_ignores_brackets_inside_strings")


# --- still fails on genuinely-broken / unrecoverable input ----------------

def test_unrecoverable_still_raises():
    # genuinely malformed-but-COMPLETE (NOT a truncation) → raise, never invent.
    for bad in ('not json', '{"a": }', '{"a" "b"}'):
        try:
            tolerant_loads(bad)
            assert False, f"expected failure for {bad!r}"
        except json.JSONDecodeError:
            pass
    print("OK test_unrecoverable_still_raises")


def test_truncated_midvalue_degrades_to_marker():
    # The VALUE itself was cut off (Tier 2): closing it would fabricate the wrong
    # argument, so instead of raising (which would crash the turn) tolerant_loads
    # returns a marker. TruncatedToolCallPlugin turns that into a clean retry.
    for bad in ('{"skill_name": "my-sk', '{"skill_name": ', '{"a": 1, "b": "cut'):
        out = tolerant_loads(bad)
        assert out == {TRUNCATED_TOOL_CALL_KEY: True}, (bad, out)
    print("OK test_truncated_midvalue_degrades_to_marker")


def test_non_str_input_passthrough():
    # non-str/bytes → behaves like stdlib (raises, not silently repaired)
    try:
        tolerant_loads(12345.0)  # not a JSON document
    except (TypeError, json.JSONDecodeError):
        pass
    print("OK test_non_str_input_passthrough")


# --- the patch actually rewires ADK lite_llm -----------------------------

def test_patch_installs_on_lite_llm():
    import adk_cc.plugins  # noqa: F401 — side-effect import triggers the patch
    from google.adk.models import lite_llm as ll
    assert isinstance(ll.json, _TolerantJsonShim), type(ll.json)
    # the patched json.loads recovers a bad-escape arg…
    assert ll.json.loads('{"content": "\\d"}')["content"] == "\\d"
    # …and json.dumps still delegates to stdlib unchanged
    assert ll.json.dumps({"a": 1}) == '{"a": 1}'
    # idempotent: re-install doesn't double-wrap
    install_tolerant_tool_json()
    assert isinstance(ll.json, _TolerantJsonShim)
    print("OK test_patch_installs_on_lite_llm")


if __name__ == "__main__":
    test_invalid_backslash_escape()
    test_raw_control_chars_in_string()
    test_trailing_comma_object_and_array()
    test_invalid_escape_and_windows_path()
    test_combined_escape_and_newline()
    test_valid_json_byte_identical()
    test_empty_and_simple()
    test_truncation_missing_brace_recovered()
    test_truncation_lone_open_brace_becomes_empty()
    test_truncation_nested_and_composes_with_repairs()
    test_completion_ignores_brackets_inside_strings()
    test_unrecoverable_still_raises()
    test_truncated_midvalue_degrades_to_marker()
    test_non_str_input_passthrough()
    test_patch_installs_on_lite_llm()
    print("\nall tolerant-tool-json tests passed")
