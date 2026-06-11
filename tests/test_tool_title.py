"""Tests for ToolTitlePlugin (plugins/tool_title.py).

The plugin injects an optional `title` arg into every tool declaration
(before_model) and strips it from args before execution (before_tool), so the
model can label calls for the UI without any tool seeing the field. Covers:

  - injection into the parameters_json_schema (AdkCcTool) form
  - injection into the types.Schema (FunctionTool) form
  - native `title` args (task_create) are NOT touched — no inject, no strip
  - `title` is never added to `required`
  - guidance appended to system_instruction, idempotently
  - strip removes title only for injected tools; AdkCcTool still validates

Hand-rolled (no pytest).
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.adk.models.llm_request import LlmRequest
from google.adk.tools.function_tool import FunctionTool

from adk_cc.plugins.tool_title import ToolTitlePlugin, TITLE_GUIDANCE
from adk_cc.tools import ReadFileTool, TaskCreateTool


def _weather(city: str) -> dict:
    """Get the weather for a city."""
    return {"city": city}


def _request_with(*tools) -> LlmRequest:
    req = LlmRequest()
    req.append_tools(list(tools))
    return req


def _decl(req: LlmRequest, name: str):
    for t in req.config.tools or []:
        for d in getattr(t, "function_declarations", []) or []:
            if d.name == name:
                return d
    raise AssertionError(f"declaration {name!r} not found")


def _props(decl) -> dict:
    if isinstance(getattr(decl, "parameters_json_schema", None), dict):
        return decl.parameters_json_schema.get("properties", {})
    return dict(decl.parameters.properties or {})


def _required(decl) -> list:
    if isinstance(getattr(decl, "parameters_json_schema", None), dict):
        return decl.parameters_json_schema.get("required", []) or []
    return list(decl.parameters.required or [])


def _run_inject(plugin: ToolTitlePlugin, req: LlmRequest) -> None:
    asyncio.run(
        plugin.before_model_callback(
            callback_context=SimpleNamespace(), llm_request=req
        )
    )


def test_injects_into_json_schema_tool():
    plugin = ToolTitlePlugin()
    req = _request_with(ReadFileTool())
    _run_inject(plugin, req)
    d = _decl(req, "read_file")
    assert "title" in _props(d), _props(d).keys()
    assert "title" not in _required(d), _required(d)
    assert "read_file" in plugin._injected
    print("OK injects_into_json_schema_tool")


def test_injects_into_types_schema_tool():
    plugin = ToolTitlePlugin()
    req = _request_with(FunctionTool(_weather))
    _run_inject(plugin, req)
    d = _decl(req, "_weather")
    assert "title" in _props(d), _props(d).keys()
    assert "title" not in _required(d), _required(d)
    print("OK injects_into_types_schema_tool")


def test_native_title_untouched():
    plugin = ToolTitlePlugin()
    req = _request_with(TaskCreateTool())
    d = _decl(req, "task_create")
    native = _props(d)["title"]  # the task title field, pre-existing
    _run_inject(plugin, req)
    assert _props(_decl(req, "task_create"))["title"] == native
    assert "task_create" not in plugin._injected
    print("OK native_title_untouched")


def test_required_never_gains_title_and_idempotent():
    plugin = ToolTitlePlugin()
    for _ in range(2):  # two fresh requests, same plugin instance
        req = _request_with(ReadFileTool(), TaskCreateTool())
        _run_inject(plugin, req)
        # INJECTED title must stay optional; task_create's NATIVE required
        # title is its own business and must remain as declared.
        assert "title" not in _required(_decl(req, "read_file"))
        assert "title" in _required(_decl(req, "task_create"))
        for name in ("read_file", "task_create"):
            # exactly ONE title property (no double-inject)
            assert list(_props(_decl(req, name)).keys()).count("title") == 1
    print("OK required_never_gains_title_and_idempotent")


def test_guidance_appended_once():
    plugin = ToolTitlePlugin()
    req = _request_with(ReadFileTool())
    req.config.system_instruction = "BASE"
    _run_inject(plugin, req)
    si = req.config.system_instruction
    assert si.startswith("BASE") and TITLE_GUIDANCE in si, si[:80]
    # idempotent on a re-run against the same request
    _run_inject(plugin, req)
    assert req.config.system_instruction.count(TITLE_GUIDANCE) == 1
    print("OK guidance_appended_once")


def test_strip_only_injected_tools():
    plugin = ToolTitlePlugin()
    read_file, task_create = ReadFileTool(), TaskCreateTool()
    _run_inject(plugin, _request_with(read_file, task_create))

    args = {"path": "/tmp/x", "title": "Reading config file"}
    asyncio.run(plugin.before_tool_callback(
        tool=read_file, tool_args=args, tool_context=None))
    assert "title" not in args, args  # injected tool -> stripped

    targs = {"title": "Run test suite", "description": "d"}
    asyncio.run(plugin.before_tool_callback(
        tool=task_create, tool_args=targs, tool_context=None))
    assert targs["title"] == "Run test suite"  # native arg -> untouched
    print("OK strip_only_injected_tools")


def test_stripped_args_still_validate():
    """After the strip, an AdkCcTool's pydantic validation sees only real args."""
    plugin = ToolTitlePlugin()
    rf = ReadFileTool()
    _run_inject(plugin, _request_with(rf))
    args = {"path": "/tmp/nope.txt", "title": "Reading a file"}
    asyncio.run(plugin.before_tool_callback(
        tool=rf, tool_args=args, tool_context=None))
    validated = rf.input_model.model_validate(args)
    assert not hasattr(validated, "title") or "title" not in args
    print("OK stripped_args_still_validate")


def main():
    test_injects_into_json_schema_tool()
    test_injects_into_types_schema_tool()
    test_native_title_untouched()
    test_required_never_gains_title_and_idempotent()
    test_guidance_appended_once()
    test_strip_only_injected_tools()
    test_stripped_args_still_validate()
    print("\nall tool-title tests passed")


if __name__ == "__main__":
    main()
