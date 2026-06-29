"""Tier 2 containment: TruncatedToolCallPlugin turns the truncation marker into a
clean retry error so a model cutoff doesn't crash the turn. Model-free.

Run: PYTHONPATH=agents .venv/bin/python tests/test_truncated_tool_call.py
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

_passed = _failed = 0


def check(name, ok):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    _passed += 1 if ok else 0
    _failed += 0 if ok else 1


def main() -> int:
    from adk_cc.plugins.truncated_tool_call import TruncatedToolCallPlugin
    from adk_cc.plugins.tolerant_tool_json import TRUNCATED_TOOL_CALL_KEY, tolerant_loads

    plugin = TruncatedToolCallPlugin()
    tool = type("T", (), {"name": "write_file"})()

    # the shim produces the marker for a tool call cut off mid-value…
    marker = tolerant_loads('{"path": "x.txt", "content": "<html')
    check("shim returns the truncation marker", marker == {TRUNCATED_TOOL_CALL_KEY: True})

    # …and the plugin short-circuits with a clean retry error (tool never runs)
    res = asyncio.run(plugin.before_tool_callback(tool=tool, tool_args=marker, tool_context=None))
    check("plugin returns an error result for the marker",
          isinstance(res, dict) and res.get("status") == "error")
    check("error names the tool + asks to resend complete args",
          "write_file" in res.get("error", "") and "COMPLETE" in res.get("error", ""))

    # normal args pass through (None → the tool runs as usual)
    res2 = asyncio.run(plugin.before_tool_callback(tool=tool, tool_args={"path": "x"}, tool_context=None))
    check("plugin returns None for normal args (tool runs)", res2 is None)

    # non-dict args don't blow up
    res3 = asyncio.run(plugin.before_tool_callback(tool=tool, tool_args=None, tool_context=None))
    check("plugin tolerates non-dict args", res3 is None)

    print(f"\ntruncated-tool-call: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
