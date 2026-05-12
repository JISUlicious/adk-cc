"""Unit tests for compaction audit events emitted by `_LazyAdkCcSummarizer`.

PR A goal: every compaction call fires `compaction_triggered` →
`compaction_success` (or `compaction_failure`) audit events so the
previously silent path is observable. Tests drive the wrap layer
through a mock summarizer to assert event ordering, field shape, and
fail-silent semantics.

Covers:
  - Success path: triggered → success with event_count + summary_bytes
    + elapsed_ms.
  - Empty-summary path: ADK's `LlmEventSummarizer` returns `None` on
    its own internal failures (no events to summarize, malformed
    response). Wrapper fires `compaction_failure` with
    `reason=empty_summary`.
  - Exception path: inner summarizer raises. Wrapper fires
    `compaction_failure` with `reason=exception` + error_type +
    error_message, then re-raises.
  - No-sink path: when no AuditPlugin is registered, the wrapper is
    silent (zero events captured) but still runs the inner call.

Run: `.venv/bin/python tests/test_compaction_audit.py`
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import patch

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.plugins.audit import (
    clear_global_sink,
    emit_compaction_event,
    is_audit_enabled,
    set_global_sink,
)


# --- Helpers -------------------------------------------------------


def _capture() -> tuple[list[dict], callable]:
    events: list[dict] = []

    def sink(event: dict) -> None:
        events.append(event)

    return events, sink


def _make_summarizer(model_id: str = "fake/model"):
    """Build a _LazyAdkCcSummarizer instance for tests.

    `_make_lazy_summarizer_class()` defers ADK imports — we call it
    here once and reuse the class across tests."""
    from adk_cc.agent import _make_lazy_summarizer_class

    cls = _make_lazy_summarizer_class()
    return cls(model_id=model_id)


class _FakeCompaction:
    """Stand-in for ADK's EventCompaction action."""

    def __init__(self, content: str) -> None:
        self.compacted_content = content


class _FakeActions:
    def __init__(self, compaction: _FakeCompaction) -> None:
        self.compaction = compaction


class _FakeReturnedEvent:
    """What ADK's `maybe_summarize_events` returns on success — an
    Event with an EventCompaction action attached. The wrapper reads
    `event.actions.compaction.compacted_content` for `summary_bytes`."""

    def __init__(self, summary: str) -> None:
        self.actions = _FakeActions(_FakeCompaction(summary))


class _FakeInputEvent:
    """Cheap stand-in for an Event in the input list; the wrapper
    only reads `.timestamp` from the last one."""

    def __init__(self, timestamp: float) -> None:
        self.timestamp = timestamp


# --- Success path --------------------------------------------------


def test_success_fires_triggered_then_success() -> None:
    """Happy path: triggered before, success after, with the documented
    fields populated."""
    events, sink = _capture()
    set_global_sink(sink)
    try:
        summarizer = _make_summarizer(model_id="openai/gpt-4o-mini")
        # Patch the inner LlmEventSummarizer to return a fake event
        # without making real LLM calls.
        async def fake_summarize(self, *, events):  # noqa: ANN001
            return _FakeReturnedEvent("compacted history " * 10)

        with patch(
            "google.adk.apps.llm_event_summarizer.LlmEventSummarizer.maybe_summarize_events",
            new=fake_summarize,
        ):
            result = asyncio.run(
                summarizer.maybe_summarize_events(
                    events=[
                        _FakeInputEvent(timestamp=1.0),
                        _FakeInputEvent(timestamp=2.0),
                        _FakeInputEvent(timestamp=3.0),
                    ]
                )
            )
    finally:
        clear_global_sink()
    assert result is not None
    # Exactly two events: triggered + success.
    assert [e["event"] for e in events] == [
        "compaction_triggered",
        "compaction_success",
    ], events
    triggered, success = events
    assert triggered["model_id"] == "openai/gpt-4o-mini"
    assert triggered["event_count"] == 3
    assert triggered["last_event_ts"] == 3.0
    assert success["model_id"] == "openai/gpt-4o-mini"
    assert success["event_count"] == 3
    assert success["summary_bytes"] > 0
    assert success["elapsed_ms"] >= 0
    print("OK test_success_fires_triggered_then_success")


# --- Empty-summary path --------------------------------------------


def test_empty_summary_fires_failure_with_empty_summary_reason() -> None:
    """When the inner summarizer returns None (ADK's silent-fail mode),
    the wrapper fires `compaction_failure` with `reason=empty_summary`
    so the operator can see the silent path."""
    events, sink = _capture()
    set_global_sink(sink)
    try:
        summarizer = _make_summarizer()

        async def fake_summarize(self, *, events):  # noqa: ANN001
            return None

        with patch(
            "google.adk.apps.llm_event_summarizer.LlmEventSummarizer.maybe_summarize_events",
            new=fake_summarize,
        ):
            result = asyncio.run(
                summarizer.maybe_summarize_events(events=[_FakeInputEvent(1.0)])
            )
    finally:
        clear_global_sink()
    assert result is None
    assert [e["event"] for e in events] == [
        "compaction_triggered",
        "compaction_failure",
    ]
    failure = events[1]
    assert failure["reason"] == "empty_summary"
    assert "elapsed_ms" in failure
    # No error_type / error_message on the empty path — it's not an
    # exception, just a None return.
    assert "error_type" not in failure
    assert "error_message" not in failure
    print("OK test_empty_summary_fires_failure_with_empty_summary_reason")


# --- Exception path ------------------------------------------------


def test_exception_fires_failure_and_returns_none() -> None:
    """PR B changed this from re-raise to graceful None-return. The
    wrapper logs WARN + emits `compaction_failure` with
    `reason=exception` and `error_type`/`error_message`, then returns
    `None`. ADK treats `None` the same as "no summary produced" — the
    turn proceeds with uncompacted history instead of the session
    hanging on a broken summarizer."""
    events, sink = _capture()
    set_global_sink(sink)
    try:
        summarizer = _make_summarizer()

        async def fake_summarize(self, *, events):  # noqa: ANN001
            raise RuntimeError("LLM backend exploded")

        with patch(
            "google.adk.apps.llm_event_summarizer.LlmEventSummarizer.maybe_summarize_events",
            new=fake_summarize,
        ):
            result = asyncio.run(
                summarizer.maybe_summarize_events(
                    events=[_FakeInputEvent(1.0)]
                )
            )
    finally:
        clear_global_sink()
    # Returns None instead of re-raising — graceful degrade.
    assert result is None
    assert [e["event"] for e in events] == [
        "compaction_triggered",
        "compaction_failure",
    ]
    failure = events[1]
    assert failure["reason"] == "exception"
    assert failure["error_type"] == "RuntimeError"
    assert "exploded" in failure["error_message"]
    assert "elapsed_ms" in failure
    print("OK test_exception_fires_failure_and_returns_none")


# --- No-sink path --------------------------------------------------


def test_no_sink_runs_silently() -> None:
    """When no AuditPlugin is registered, the wrapper still runs the
    inner call but emits nothing. Zero overhead for operators not
    using audit."""
    clear_global_sink()
    assert not is_audit_enabled()
    summarizer = _make_summarizer()

    async def fake_summarize(self, *, events):  # noqa: ANN001
        return _FakeReturnedEvent("anything")

    with patch(
        "google.adk.apps.llm_event_summarizer.LlmEventSummarizer.maybe_summarize_events",
        new=fake_summarize,
    ):
        result = asyncio.run(
            summarizer.maybe_summarize_events(events=[_FakeInputEvent(1.0)])
        )
    # Inner call succeeded; no events were emitted because no sink.
    assert result is not None
    print("OK test_no_sink_runs_silently")


# --- emit_compaction_event helper ----------------------------------


def test_emit_compaction_event_helper() -> None:
    """`emit_compaction_event(event_type, **fields)` builds an event
    with the documented shape: `ts`, `event`, and any extra kwargs as
    top-level fields. No-op without a sink."""
    # No-sink path.
    clear_global_sink()
    emit_compaction_event("compaction_triggered", model_id="x")  # must not raise

    # With sink.
    events, sink = _capture()
    set_global_sink(sink)
    try:
        emit_compaction_event(
            "compaction_success",
            model_id="openai/gpt-4o-mini",
            summary_bytes=512,
            elapsed_ms=1234,
        )
    finally:
        clear_global_sink()
    assert len(events) == 1
    e = events[0]
    assert e["event"] == "compaction_success"
    assert e["model_id"] == "openai/gpt-4o-mini"
    assert e["summary_bytes"] == 512
    assert e["elapsed_ms"] == 1234
    assert isinstance(e["ts"], (int, float))
    print("OK test_emit_compaction_event_helper")


# --- Timeout path (PR B) -------------------------------------------


def test_timeout_fires_failure_with_timeout_reason_and_returns_none() -> None:
    """When the inner summarizer takes longer than `timeout_seconds`,
    `asyncio.wait_for` raises `TimeoutError`. The wrapper catches it,
    emits `compaction_failure` with `reason=timeout`, and returns
    `None` so the turn proceeds with uncompacted history rather than
    the session hanging on a stuck summarizer."""
    events, sink = _capture()
    set_global_sink(sink)
    try:
        # 50ms timeout; the fake summarizer sleeps 500ms — guaranteed timeout.
        summarizer = _make_summarizer()
        summarizer.timeout_seconds = 0.05

        async def slow_summarize(self, *, events):  # noqa: ANN001
            await asyncio.sleep(0.5)
            return _FakeReturnedEvent("never reached")

        with patch(
            "google.adk.apps.llm_event_summarizer.LlmEventSummarizer.maybe_summarize_events",
            new=slow_summarize,
        ):
            result = asyncio.run(
                summarizer.maybe_summarize_events(
                    events=[_FakeInputEvent(1.0)]
                )
            )
    finally:
        clear_global_sink()
    assert result is None
    assert [e["event"] for e in events] == [
        "compaction_triggered",
        "compaction_failure",
    ]
    failure = events[1]
    assert failure["reason"] == "timeout"
    assert failure["timeout_seconds"] == 0.05
    assert "elapsed_ms" in failure
    # Triggered event carries the timeout config too, so consumers can
    # tell from the trail whether the summarizer was running unbounded.
    assert events[0]["timeout_seconds"] == 0.05
    print("OK test_timeout_fires_failure_with_timeout_reason_and_returns_none")


def test_timeout_zero_disables_wait_for() -> None:
    """`timeout_seconds=0` opts out of the timeout entirely — the
    inner call runs unbounded. Verified by a slow summarizer that
    completes successfully because nothing is interrupting it."""
    events, sink = _capture()
    set_global_sink(sink)
    try:
        summarizer = _make_summarizer()
        summarizer.timeout_seconds = 0

        async def slow_but_successful(self, *, events):  # noqa: ANN001
            await asyncio.sleep(0.05)
            return _FakeReturnedEvent("eventually succeeded")

        with patch(
            "google.adk.apps.llm_event_summarizer.LlmEventSummarizer.maybe_summarize_events",
            new=slow_but_successful,
        ):
            result = asyncio.run(
                summarizer.maybe_summarize_events(
                    events=[_FakeInputEvent(1.0)]
                )
            )
    finally:
        clear_global_sink()
    assert result is not None
    # Success path, NOT a timeout failure.
    assert [e["event"] for e in events] == [
        "compaction_triggered",
        "compaction_success",
    ]
    # The triggered event reports timeout_seconds=0 so audit consumers
    # can confirm the unbounded-wait config was in effect.
    assert events[0]["timeout_seconds"] == 0
    print("OK test_timeout_zero_disables_wait_for")


# --- Env-var loading (PR B) ----------------------------------------


def test_env_var_loads_timeout_default_30() -> None:
    """ADK_CC_COMPACTION_TIMEOUT_S unset → default 30s. Verified by
    constructing through `_make_compaction_summarizer`."""
    import os
    from adk_cc.agent import _make_compaction_summarizer

    # Save + clear the env var so the default kicks in.
    saved = os.environ.pop("ADK_CC_COMPACTION_TIMEOUT_S", None)
    try:
        s = _make_compaction_summarizer()
        assert s.timeout_seconds == 30.0
    finally:
        if saved is not None:
            os.environ["ADK_CC_COMPACTION_TIMEOUT_S"] = saved
    print("OK test_env_var_loads_timeout_default_30")


def test_env_var_loads_timeout_explicit() -> None:
    """Explicit ADK_CC_COMPACTION_TIMEOUT_S parses cleanly. Values
    include 0 (disabled), positive ints, and floats."""
    import os
    from adk_cc.agent import _make_compaction_summarizer

    saved = os.environ.pop("ADK_CC_COMPACTION_TIMEOUT_S", None)
    try:
        for raw, expected in (("0", 0.0), ("60", 60.0), ("0.5", 0.5)):
            os.environ["ADK_CC_COMPACTION_TIMEOUT_S"] = raw
            s = _make_compaction_summarizer()
            assert s.timeout_seconds == expected, (raw, s.timeout_seconds)
    finally:
        if saved is not None:
            os.environ["ADK_CC_COMPACTION_TIMEOUT_S"] = saved
        else:
            os.environ.pop("ADK_CC_COMPACTION_TIMEOUT_S", None)
    print("OK test_env_var_loads_timeout_explicit")


def test_env_var_invalid_falls_back_to_default() -> None:
    """A typo'd value (`"abc"`, negative number) falls back to the
    default. Agent boot mustn't die on a env-config misspelling."""
    import os
    from adk_cc.agent import _make_compaction_summarizer

    saved = os.environ.pop("ADK_CC_COMPACTION_TIMEOUT_S", None)
    try:
        # Garbage string → fall back to 30.
        os.environ["ADK_CC_COMPACTION_TIMEOUT_S"] = "not-a-number"
        s = _make_compaction_summarizer()
        assert s.timeout_seconds == 30.0
        # Negative number → clamped to 0 (disabled).
        os.environ["ADK_CC_COMPACTION_TIMEOUT_S"] = "-5"
        s = _make_compaction_summarizer()
        assert s.timeout_seconds == 0.0
    finally:
        if saved is not None:
            os.environ["ADK_CC_COMPACTION_TIMEOUT_S"] = saved
        else:
            os.environ.pop("ADK_CC_COMPACTION_TIMEOUT_S", None)
    print("OK test_env_var_invalid_falls_back_to_default")


# --- Driver --------------------------------------------------------


def main() -> None:
    test_success_fires_triggered_then_success()
    test_empty_summary_fires_failure_with_empty_summary_reason()
    test_exception_fires_failure_and_returns_none()
    test_no_sink_runs_silently()
    test_emit_compaction_event_helper()
    test_timeout_fires_failure_with_timeout_reason_and_returns_none()
    test_timeout_zero_disables_wait_for()
    test_env_var_loads_timeout_default_30()
    test_env_var_loads_timeout_explicit()
    test_env_var_invalid_falls_back_to_default()
    print("\nall compaction-audit tests passed")


if __name__ == "__main__":
    main()
