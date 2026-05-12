"""Unit tests for `ModelIOTracePlugin`.

Covers:
  - Plugin is a no-op when disabled (default), so the per-turn cost
    is one attribute check.
  - When enabled, before_model_callback emits a DEBUG log + a
    `model_request` audit event with the documented field shape.
  - When enabled, after_model_callback emits `model_response` —
    AND skips partial chunks so only the aggregated final response
    lands in the trail.
  - Truncation kicks in at `max_bytes`, with the `truncated` marker
    set and original byte count preserved.
  - Serialization failures don't crash the plugin (fail-silent like
    the rest of the audit machinery).

Run: `.venv/bin/python tests/test_model_io_trace.py`
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from pydantic import BaseModel

from adk_cc.plugins.audit import clear_global_sink, set_global_sink
from adk_cc.plugins.model_io_trace import (
    ModelIOTracePlugin,
    _truncate_json,
)


# --- Fakes ---------------------------------------------------------


class _FakeContent(BaseModel):
    role: str
    parts: list[dict[str, Any]] = []


class _FakeRequest(BaseModel):
    model: str = "fake/model"
    contents: list[_FakeContent] = []
    config: Optional[dict[str, Any]] = None


class _FakeResponse(BaseModel):
    content: Optional[_FakeContent] = None
    partial: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class _FakeCtx:
    """Minimal stand-in for CallbackContext."""

    def __init__(self) -> None:
        self.agent_name = "coordinator"
        self.invocation_id = "inv-1"


def _run_before(plugin: ModelIOTracePlugin, req: Any) -> None:
    asyncio.run(
        plugin.before_model_callback(
            callback_context=_FakeCtx(), llm_request=req
        )
    )


def _run_after(plugin: ModelIOTracePlugin, resp: Any) -> None:
    asyncio.run(
        plugin.after_model_callback(
            callback_context=_FakeCtx(), llm_response=resp
        )
    )


def _capture() -> tuple[list[dict], callable]:
    events: list[dict] = []

    def sink(event: dict) -> None:
        events.append(event)

    return events, sink


# --- Disabled path -------------------------------------------------


def test_disabled_no_emit() -> None:
    """Default-off: no events, no log activity. The plugin is wired
    unconditionally in agent.py, so the cheapest possible path matters."""
    events, sink = _capture()
    set_global_sink(sink)
    try:
        plugin = ModelIOTracePlugin(enabled=False)
        _run_before(plugin, _FakeRequest(contents=[_FakeContent(role="user", parts=[{"text": "hi"}])]))
        _run_after(plugin, _FakeResponse(content=_FakeContent(role="model", parts=[{"text": "ok"}])))
    finally:
        clear_global_sink()
    assert events == []
    print("OK test_disabled_no_emit")


# --- Enabled — request side ----------------------------------------


def test_enabled_emits_model_request_event() -> None:
    events, sink = _capture()
    set_global_sink(sink)
    try:
        plugin = ModelIOTracePlugin(enabled=True)
        req = _FakeRequest(
            contents=[
                _FakeContent(role="user", parts=[{"text": "rm /tmp/x"}]),
                _FakeContent(role="model", parts=[{"text": "ok"}]),
            ],
            config={
                "tools": [
                    {
                        "function_declarations": [
                            {"name": "run_bash", "description": "..."},
                            {"name": "read_file", "description": "..."},
                        ]
                    }
                ]
            },
        )
        _run_before(plugin, req)
    finally:
        clear_global_sink()
    assert len(events) == 1, events
    e = events[0]
    assert e["event"] == "model_request"
    assert e["model"] == "fake/model"
    assert e["content_turns"] == 2
    assert e["tool_count"] == 2
    assert e["truncated"] is False
    # Context fields surface from the fake callback context.
    assert e["agent_name"] == "coordinator"
    assert e["invocation_id"] == "inv-1"
    # The serialized payload roundtrips back to JSON.
    payload = json.loads(e["payload"])
    assert payload["model"] == "fake/model"
    assert len(payload["contents"]) == 2
    print("OK test_enabled_emits_model_request_event")


# --- Enabled — response side ---------------------------------------


def test_enabled_emits_model_response_event() -> None:
    events, sink = _capture()
    set_global_sink(sink)
    try:
        plugin = ModelIOTracePlugin(enabled=True)
        resp = _FakeResponse(
            content=_FakeContent(
                role="model",
                parts=[{"text": "running"}, {"function_call": {"name": "run_bash"}}],
            ),
        )
        _run_after(plugin, resp)
    finally:
        clear_global_sink()
    assert len(events) == 1, events
    e = events[0]
    assert e["event"] == "model_response"
    assert e["parts_count"] == 2
    assert e["error_code"] is None
    payload = json.loads(e["payload"])
    assert payload["content"]["role"] == "model"
    print("OK test_enabled_emits_model_response_event")


def test_after_skips_partial_chunks() -> None:
    """ADK fires after_model_callback once per streamed chunk. Logging
    every chunk would balloon the trail; the plugin skips partials so
    only the final aggregated response lands."""
    events, sink = _capture()
    set_global_sink(sink)
    try:
        plugin = ModelIOTracePlugin(enabled=True)
        # 5 partial chunks + 1 final.
        for _ in range(5):
            _run_after(plugin, _FakeResponse(partial=True))
        _run_after(plugin, _FakeResponse(partial=False, content=_FakeContent(role="model")))
    finally:
        clear_global_sink()
    # Only the non-partial chunk produces an event.
    assert len(events) == 1, events
    assert events[0]["event"] == "model_response"
    print("OK test_after_skips_partial_chunks")


# --- Truncation ----------------------------------------------------


def test_truncate_marks_oversize_payloads() -> None:
    events, sink = _capture()
    set_global_sink(sink)
    try:
        plugin = ModelIOTracePlugin(enabled=True, max_bytes=200)
        # Huge prompt.
        big_text = "x" * 5000
        req = _FakeRequest(contents=[_FakeContent(role="user", parts=[{"text": big_text}])])
        _run_before(plugin, req)
    finally:
        clear_global_sink()
    e = events[0]
    assert e["truncated"] is True, e
    # Original byte count preserved so the operator knows how big it
    # actually was, even though the stored text is cut.
    assert e["payload_bytes"] > 200
    assert len(e["payload"].encode("utf-8")) <= 200
    print("OK test_truncate_marks_oversize_payloads")


def test_truncate_helper_unit() -> None:
    """`_truncate_json` directly — when under the cap nothing is
    truncated; when over, text is cut and truncated=True."""
    out = _truncate_json({"a": "b"}, max_bytes=1000)
    assert out["truncated"] is False
    assert out["bytes"] < 50

    out2 = _truncate_json({"a": "x" * 5000}, max_bytes=100)
    assert out2["truncated"] is True
    assert out2["bytes"] > 100
    assert len(out2["text"].encode("utf-8")) <= 100
    print("OK test_truncate_helper_unit")


def test_serialization_failure_does_not_crash() -> None:
    """A non-serializable payload (cyclic refs, file handles, ...)
    must NOT crash the model call. The plugin's fail-silent
    discipline matches AuditPlugin._emit."""

    # Construct an object json.dumps can't handle without a default
    # callback. We use `default=str` in the plugin, so almost anything
    # serializes via repr — but bytes inside a list-with-Pydantic
    # mock can still trip serialization. Use a class with __getstate__
    # raising to force the failure.
    class _Boom:
        def __repr__(self) -> str:
            raise RuntimeError("repr exploded")

    out = _truncate_json({"weird": _Boom()}, max_bytes=200)
    # Should NOT raise — returns a fallback marker.
    assert "serialization failed" in out["text"]
    print("OK test_serialization_failure_does_not_crash")


# --- Driver --------------------------------------------------------


def main() -> None:
    test_disabled_no_emit()
    test_enabled_emits_model_request_event()
    test_enabled_emits_model_response_event()
    test_after_skips_partial_chunks()
    test_truncate_marks_oversize_payloads()
    test_truncate_helper_unit()
    test_serialization_failure_does_not_crash()
    print("\nall model-io-trace tests passed")


if __name__ == "__main__":
    main()
