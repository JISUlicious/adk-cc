"""Tests for SetSessionTitleTool (tools/set_session_title.py).

Writes state["session_title"] for the UI session rail. Hand-rolled.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.tools.set_session_title import SetSessionTitleArgs, SetSessionTitleTool


def _ctx():
    return SimpleNamespace(state={})


def _run(tool, ctx, title):
    return asyncio.run(tool._execute(SetSessionTitleArgs(title=title), ctx))


def test_sets_and_normalizes():
    tool, ctx = SetSessionTitleTool(), _ctx()
    out = _run(tool, ctx, "  Fizzbuzz   script\n demo ")
    assert out["status"] == "ok", out
    assert ctx.state["session_title"] == "Fizzbuzz script demo", ctx.state
    assert out["previous"] is None
    print("OK sets_and_normalizes")


def test_overwrite_reports_previous():
    tool, ctx = SetSessionTitleTool(), _ctx()
    _run(tool, ctx, "First topic")
    out = _run(tool, ctx, "Second topic")
    assert out["status"] == "ok" and out["previous"] == "First topic", out
    assert ctx.state["session_title"] == "Second topic"
    print("OK overwrite_reports_previous")


def test_overlong_truncated():
    tool, ctx = SetSessionTitleTool(), _ctx()
    out = _run(tool, ctx, "x" * 300)
    assert out["status"] == "ok"
    t = ctx.state["session_title"]
    assert len(t) <= 80 and t.endswith("…"), (len(t), t[-5:])
    print("OK overlong_truncated")


def test_blank_rejected():
    tool, ctx = SetSessionTitleTool(), _ctx()
    out = _run(tool, ctx, "   ")
    assert out["status"] == "error", out
    assert "session_title" not in ctx.state
    print("OK blank_rejected")


def main():
    test_sets_and_normalizes()
    test_overwrite_reports_previous()
    test_overlong_truncated()
    test_blank_rejected()
    print("\nall set-session-title tests passed")


if __name__ == "__main__":
    main()
