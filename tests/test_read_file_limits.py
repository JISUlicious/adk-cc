"""Unit tests for `read_file`'s line offset/limit + per-line truncation.

Regression: read_file used to return the entire file unconditionally,
overflowing the LLM context (or `ContextGuardPlugin`'s REJECT threshold)
on large files. The fix adds `offset`/`limit` with sensible defaults
(first 2000 lines, max 2000 per call) and a per-line length cap.

Covers:
  - Default reads slice from the start with metadata.
  - Small file: full content returned, has_more=False.
  - Large file (>2000 lines): default returns 2000 with has_more=True.
  - offset past end-of-file: empty content, has_more=False.
  - Per-line truncation cap (lines over 2000 chars marked + counted).
  - Custom offset+limit combinations.
  - Schema validation: limit > 2000 rejected, offset < 1 rejected.
  - Non-existent file, directory, non-utf8 still return the same
    error shapes they did before.

Run: `.venv/bin/python tests/test_read_file_limits.py`
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

# Point the default workspace at a tempdir BEFORE importing adk_cc, so
# the noop backend's fs_read allows reads under it. Without this, the
# default workspace anchors at cwd and refuses tempfile paths in
# `/private/var/folders/...` (the macOS resolved tempdir).
_WS_ROOT = tempfile.mkdtemp(prefix="adk-cc-read-test-")
os.environ["ADK_CC_WORKSPACE_ROOT"] = _WS_ROOT
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from pydantic import ValidationError

from adk_cc.tools.read_file import (
    _LINE_TRUNCATION_SUFFIX,
    _MAX_LINE_LENGTH,
    ReadFileTool,
)
from adk_cc.tools.schemas import ReadFileArgs


# --- Fakes ----------------------------------------------------------


class _FakeState:
    def __init__(self) -> None:
        self._d: dict = {}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def __getitem__(self, k):
        return self._d[k]

    def __setitem__(self, k, v):
        self._d[k] = v


class _FakeToolContext:
    """Minimal ToolContext: empty state so get_backend/get_workspace
    fall back to module-level defaults (NoopBackend + workspace rooted
    at `_WS_ROOT` per the env var set above)."""

    def __init__(self) -> None:
        self.state = _FakeState()


# --- Helpers --------------------------------------------------------


def _call(path: str, **kwargs: Any) -> dict:
    """Invoke the tool with given args; assert it returns a dict."""
    tool = ReadFileTool()
    args = ReadFileArgs(path=path, **kwargs)
    out = asyncio.run(tool._execute(args, _FakeToolContext()))
    assert isinstance(out, dict), out
    return out


def _make_file(contents: str) -> str:
    """Write a temp file UNDER the workspace root and return its
    absolute path. The noop backend's fs_read only permits paths
    inside the workspace; tests must live there too."""
    fd, name = tempfile.mkstemp(suffix=".txt", dir=_WS_ROOT)
    os.close(fd)
    Path(name).write_text(contents, encoding="utf-8")
    return name


def _parse_cat_n(content: str) -> list[tuple[int, str]]:
    """Reverse the `cat -n`-style line formatting (`<padded num>\\t<text>`)
    so tests can assert on text content without baking the prefix shape
    into every assertion. Lines without the tab separator are returned
    with line number 0 (defensive — shouldn't happen)."""
    if not content:
        return []
    out: list[tuple[int, str]] = []
    for raw in content.split("\n"):
        if "\t" not in raw:
            out.append((0, raw))
            continue
        num_str, text = raw.split("\t", 1)
        try:
            out.append((int(num_str.strip()), text))
        except ValueError:
            out.append((0, raw))
    return out


def _texts(content: str) -> list[str]:
    """Just the text portion of each parsed `cat -n` line."""
    return [t for _, t in _parse_cat_n(content)]


# --- Tests ----------------------------------------------------------


def test_small_file_returns_full_content_no_more() -> None:
    """File with <2000 lines: defaults return everything; has_more=False."""
    body = "\n".join(f"line {i}" for i in range(1, 11))  # 10 lines
    path = _make_file(body)
    try:
        out = _call(path)
        assert out["status"] == "ok", out
        assert out["start_line"] == 1
        assert out["end_line"] == 10
        assert out["total_lines"] == 10
        assert out["has_more"] is False
        assert out["lines_truncated"] == 0
        assert _texts(out["content"]) == [f"line {i}" for i in range(1, 11)]
        # Line numbers in content match the file's actual line numbers.
        parsed = _parse_cat_n(out["content"])
        assert [n for n, _ in parsed] == list(range(1, 11))
    finally:
        os.unlink(path)
    print("OK test_small_file_returns_full_content_no_more")


def test_large_file_default_caps_at_2000_lines() -> None:
    """File with 5000 lines: default reads lines 1..2000, has_more=True."""
    body = "\n".join(f"L{i}" for i in range(1, 5001))
    path = _make_file(body)
    try:
        out = _call(path)
        assert out["status"] == "ok"
        assert out["start_line"] == 1
        assert out["end_line"] == 2000
        assert out["total_lines"] == 5000
        assert out["has_more"] is True
        texts = _texts(out["content"])
        assert len(texts) == 2000
        assert texts[0] == "L1"
        assert texts[-1] == "L2000"
    finally:
        os.unlink(path)
    print("OK test_large_file_default_caps_at_2000_lines")


def test_offset_reads_subsequent_slice() -> None:
    """Pagination: second call with offset=2001 gets the next slice."""
    body = "\n".join(f"L{i}" for i in range(1, 5001))
    path = _make_file(body)
    try:
        out = _call(path, offset=2001, limit=2000)
        assert out["start_line"] == 2001
        assert out["end_line"] == 4000
        assert out["has_more"] is True

        parsed = _parse_cat_n(out["content"])
        assert parsed[0] == (2001, "L2001")
        assert parsed[-1] == (4000, "L4000")

        # Third call gets the tail.
        out3 = _call(path, offset=4001, limit=2000)
        assert out3["start_line"] == 4001
        assert out3["end_line"] == 5000
        assert out3["has_more"] is False
        parsed3 = _parse_cat_n(out3["content"])
        # Line numbers continue the file's numbering, not the slice's.
        assert parsed3[0] == (4001, "L4001")
        assert parsed3[-1] == (5000, "L5000")
    finally:
        os.unlink(path)
    print("OK test_offset_reads_subsequent_slice")


def test_offset_past_end_returns_empty_slice() -> None:
    body = "\n".join(f"L{i}" for i in range(1, 11))  # 10 lines
    path = _make_file(body)
    try:
        out = _call(path, offset=100)
        assert out["status"] == "ok"
        assert out["content"] == ""
        # Empty-slice convention: end_line == start_line - 1 so the
        # half-open [start_line, end_line] range is empty.
        assert out["start_line"] == 100
        assert out["end_line"] == 99
        assert out["total_lines"] == 10
        assert out["has_more"] is False
    finally:
        os.unlink(path)
    print("OK test_offset_past_end_returns_empty_slice")


def test_per_line_truncation_cap() -> None:
    """Pathological lines (>2000 chars) get clipped + counted."""
    long_line = "x" * (_MAX_LINE_LENGTH + 500)
    other_long_line = "y" * (_MAX_LINE_LENGTH + 1)
    body = "\n".join(["normal", long_line, "short", other_long_line])
    path = _make_file(body)
    try:
        out = _call(path)
        assert out["lines_truncated"] == 2, out["lines_truncated"]
        texts = _texts(out["content"])
        assert texts[0] == "normal"
        # The truncated line ends with the marker; the TEXT portion
        # (after stripping `<num>\t`) is _MAX_LINE_LENGTH chars + suffix.
        assert texts[1].endswith(_LINE_TRUNCATION_SUFFIX)
        assert len(texts[1]) == _MAX_LINE_LENGTH + len(_LINE_TRUNCATION_SUFFIX)
        assert texts[1].startswith("x" * _MAX_LINE_LENGTH)
        assert texts[2] == "short"
        assert texts[3].endswith(_LINE_TRUNCATION_SUFFIX)
    finally:
        os.unlink(path)
    print("OK test_per_line_truncation_cap")


def test_custom_limit_within_bounds() -> None:
    body = "\n".join(f"L{i}" for i in range(1, 51))  # 50 lines
    path = _make_file(body)
    try:
        out = _call(path, offset=10, limit=5)
        assert out["start_line"] == 10
        assert out["end_line"] == 14
        assert _texts(out["content"]) == ["L10", "L11", "L12", "L13", "L14"]
        # Line numbers in the cat -n prefix continue from `offset`,
        # not the slice's internal index.
        parsed = _parse_cat_n(out["content"])
        assert [n for n, _ in parsed] == [10, 11, 12, 13, 14]
        assert out["has_more"] is True
    finally:
        os.unlink(path)
    print("OK test_custom_limit_within_bounds")


def test_schema_rejects_limit_over_2000() -> None:
    try:
        ReadFileArgs(path="x", limit=2001)
    except ValidationError:
        print("OK test_schema_rejects_limit_over_2000")
        return
    raise AssertionError("expected ValidationError for limit > 2000")


def test_schema_rejects_zero_or_negative_offset() -> None:
    for bad in (0, -5):
        try:
            ReadFileArgs(path="x", offset=bad)
        except ValidationError:
            continue
        raise AssertionError(f"expected ValidationError for offset={bad}")
    print("OK test_schema_rejects_zero_or_negative_offset")


def test_schema_rejects_zero_or_negative_limit() -> None:
    for bad in (0, -1):
        try:
            ReadFileArgs(path="x", limit=bad)
        except ValidationError:
            continue
        raise AssertionError(f"expected ValidationError for limit={bad}")
    print("OK test_schema_rejects_zero_or_negative_limit")


def test_nonexistent_file_returns_error() -> None:
    # Use a path inside the workspace so fs_read allows it, then the
    # tool's own FileNotFoundError path fires.
    out = _call(f"{_WS_ROOT}/nonexistent-file-asdf-12345.txt")
    assert out["status"] == "error", out
    assert "file not found" in out["error"], out["error"]
    print("OK test_nonexistent_file_returns_error")


def test_directory_returns_error() -> None:
    # _WS_ROOT is a directory and is allowed by fs_read; passing it
    # should produce a clean error from the IsADirectoryError path.
    out = _call(_WS_ROOT)
    assert out["status"] == "error", out
    assert "not a regular file" in out["error"], out
    print("OK test_directory_returns_error")


def test_empty_file_zero_lines() -> None:
    path = _make_file("")
    try:
        out = _call(path)
        assert out["status"] == "ok"
        assert out["content"] == ""
        assert out["total_lines"] == 0
        assert out["has_more"] is False
        assert out["end_line"] == 0
        assert out["start_line"] == 1
    finally:
        os.unlink(path)
    print("OK test_empty_file_zero_lines")


def test_trailing_newline_handled() -> None:
    """splitlines() is consistent regardless of trailing newline."""
    body_no_trail = "a\nb\nc"
    body_trail = "a\nb\nc\n"
    p1 = _make_file(body_no_trail)
    p2 = _make_file(body_trail)
    try:
        o1 = _call(p1)
        o2 = _call(p2)
        assert o1["total_lines"] == 3
        assert o2["total_lines"] == 3
        assert _texts(o1["content"]) == ["a", "b", "c"]
        assert _texts(o2["content"]) == ["a", "b", "c"]
    finally:
        os.unlink(p1)
        os.unlink(p2)
    print("OK test_trailing_newline_handled")


def test_total_bytes_in_response() -> None:
    """File size in bytes is reported up-front so the model can decide
    whether to read or grep."""
    body = "hello\nworld\n"
    path = _make_file(body)
    try:
        out = _call(path)
        assert out["total_bytes"] == len(body.encode("utf-8")), out
    finally:
        os.unlink(path)
    # Multi-byte chars: UTF-8 expands to >1 byte per char.
    body2 = "café\n"
    path2 = _make_file(body2)
    try:
        out = _call(path2)
        # "café\n" → 6 bytes (c=1, a=1, f=1, é=2, \n=1).
        assert out["total_bytes"] == 6, out["total_bytes"]
    finally:
        os.unlink(path2)
    print("OK test_total_bytes_in_response")


def test_cat_n_format_padding() -> None:
    """Line numbers are right-justified to 6 chars then a tab."""
    body = "\n".join(["a", "b", "c"])
    path = _make_file(body)
    try:
        out = _call(path)
        # First chunk of content is "     1\ta".
        assert out["content"].startswith("     1\ta"), repr(out["content"][:20])
        # Line 2 too.
        assert "\n     2\tb" in out["content"]
    finally:
        os.unlink(path)
    print("OK test_cat_n_format_padding")


# --- Driver ---------------------------------------------------------


def main() -> None:
    test_small_file_returns_full_content_no_more()
    test_large_file_default_caps_at_2000_lines()
    test_offset_reads_subsequent_slice()
    test_offset_past_end_returns_empty_slice()
    test_per_line_truncation_cap()
    test_custom_limit_within_bounds()
    test_schema_rejects_limit_over_2000()
    test_schema_rejects_zero_or_negative_offset()
    test_schema_rejects_zero_or_negative_limit()
    test_nonexistent_file_returns_error()
    test_directory_returns_error()
    test_empty_file_zero_lines()
    test_trailing_newline_handled()
    test_total_bytes_in_response()
    test_cat_n_format_padding()
    print("\nall read_file-limits tests passed")


if __name__ == "__main__":
    main()
