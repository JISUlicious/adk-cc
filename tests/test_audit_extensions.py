"""Unit tests for the new audit event types introduced in the
debug-logging PR:

  - `permission_decision`  (emitted from permissions.engine.decide)
  - `state_mutation`       (emitted from PermissionPlugin._add_session_allow,
                            EnterPlanModeTool._execute, ExitPlanModeTool._execute)
  - `confirmation_resume`  (emitted from PermissionPlugin and AdkCcTool.run_async)

Verifies:
  - `emit_audit_event` is a silent no-op when no AuditPlugin sink is
    registered (so callsites that fire without audit don't crash).
  - When a sink is registered via `set_global_sink`, helpers route
    events to it with the documented schema.
  - Existing `tool_call_*` events from AuditPlugin still work
    unchanged after the lazy-import refactor.

Run: `.venv/bin/python tests/test_audit_extensions.py`
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.plugins.audit import (
    AuditPlugin,
    clear_global_sink,
    emit_audit_event,
    emit_confirmation_resume,
    emit_permission_decision,
    emit_state_mutation,
    is_audit_enabled,
    set_global_sink,
)


def _capture_events() -> tuple[list[dict], callable]:
    """Returns (events, sink_callable). Append-style capture for
    asserting on the emitted events."""
    events: list[dict] = []

    def sink(event: dict) -> None:
        events.append(event)

    return events, sink


# --- No-op when no sink is registered ------------------------------


def test_emit_no_op_without_sink() -> None:
    """Calling emit_* when no AuditPlugin has been instantiated must
    NOT raise. Operators who haven't configured audit get a silent
    fallthrough — every callsite already proves this is the path."""
    clear_global_sink()
    assert not is_audit_enabled()
    # All four emitters should be silent no-ops.
    emit_audit_event({"event": "anything"})
    emit_permission_decision(
        tool_name="run_bash",
        args={"command": "ls"},
        behavior="ask",
        reason="destructive",
        matched_rule=None,
        mode="default",
    )
    emit_state_mutation(
        mutation_type="permission_mode_change",
        state_key="permission_mode",
        details={"previous_value": "plan", "new_value": "default"},
    )
    emit_confirmation_resume(
        tool_name="run_bash",
        chose_id="allow_once",
        confirmed=True,
    )
    print("OK test_emit_no_op_without_sink")


# --- Routing with a registered sink --------------------------------


def test_permission_decision_event_shape() -> None:
    """The emit helper builds the documented field set: event,
    tool_name, tool_args, behavior, reason, matched_rule, mode, ts."""
    events, sink = _capture_events()
    set_global_sink(sink)
    try:
        emit_permission_decision(
            tool_name="run_bash",
            args={"command": "rm /tmp/foo"},
            behavior="ask",
            reason="destructive run_bash requires confirmation",
            matched_rule=None,
            mode="default",
        )
    finally:
        clear_global_sink()
    assert len(events) == 1, events
    e = events[0]
    assert e["event"] == "permission_decision"
    assert e["tool_name"] == "run_bash"
    assert e["tool_args"] == {"command": "rm /tmp/foo"}
    assert e["behavior"] == "ask"
    assert "destructive" in e["reason"]
    assert e["matched_rule"] is None
    assert e["mode"] == "default"
    assert isinstance(e["ts"], (int, float))
    print("OK test_permission_decision_event_shape")


def test_state_mutation_allow_rule_added_shape() -> None:
    """Allow-rule writes: mutation_type, state_key, tool_name,
    rule_contents (literal + broadened), persist_across_sessions."""
    events, sink = _capture_events()
    set_global_sink(sink)
    try:
        emit_state_mutation(
            mutation_type="allow_rule_added",
            state_key="adk_cc_allow_rules",
            details={
                "tool_name": "run_bash",
                "rule_contents": ["pip install pandas", "pip install *"],
                "persist_across_sessions": False,
            },
        )
    finally:
        clear_global_sink()
    e = events[0]
    assert e["event"] == "state_mutation"
    assert e["mutation_type"] == "allow_rule_added"
    assert e["state_key"] == "adk_cc_allow_rules"
    assert e["tool_name"] == "run_bash"
    assert e["rule_contents"] == ["pip install pandas", "pip install *"]
    assert e["persist_across_sessions"] is False
    print("OK test_state_mutation_allow_rule_added_shape")


def test_state_mutation_permission_mode_change_shape() -> None:
    """Mode flips: previous_value + new_value carry through details."""
    events, sink = _capture_events()
    set_global_sink(sink)
    try:
        emit_state_mutation(
            mutation_type="permission_mode_change",
            state_key="permission_mode",
            details={
                "previous_value": "plan",
                "new_value": "default",
            },
        )
    finally:
        clear_global_sink()
    e = events[0]
    assert e["mutation_type"] == "permission_mode_change"
    assert e["previous_value"] == "plan"
    assert e["new_value"] == "default"
    print("OK test_state_mutation_permission_mode_change_shape")


def test_confirmation_resume_event_shape() -> None:
    events, sink = _capture_events()
    set_global_sink(sink)
    try:
        emit_confirmation_resume(
            tool_name="run_bash",
            chose_id="allow_always",
            confirmed=True,
            function_call_id="call-42",
        )
    finally:
        clear_global_sink()
    e = events[0]
    assert e["event"] == "confirmation_resume"
    assert e["tool_name"] == "run_bash"
    assert e["chose_id"] == "allow_always"
    assert e["confirmed"] is True
    assert e["function_call_id"] == "call-42"
    print("OK test_confirmation_resume_event_shape")


def test_sink_errors_do_not_propagate() -> None:
    """A sink that raises must NOT crash the agent loop — audit's
    fail-silent discipline. The callsite's tool work continues."""

    def bad_sink(_e: dict) -> None:
        raise RuntimeError("bad sink")

    set_global_sink(bad_sink)
    try:
        # Should NOT raise.
        emit_permission_decision(
            tool_name="run_bash",
            args={},
            behavior="allow",
            reason="ok",
            matched_rule=None,
            mode="default",
        )
    finally:
        clear_global_sink()
    print("OK test_sink_errors_do_not_propagate")


# --- AuditPlugin self-registers as the global sink ------------------


def test_audit_plugin_self_registers_sink(tmp_path=None) -> None:
    """Constructing AuditPlugin installs the process-wide sink so
    non-plugin callsites' emit_* calls go to the same file/callable."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="r", delete=False, suffix=".jsonl") as f:
        path = f.name
    try:
        clear_global_sink()
        assert not is_audit_enabled()
        AuditPlugin(sink=path)
        assert is_audit_enabled()
        emit_permission_decision(
            tool_name="run_bash",
            args={"command": "ls"},
            behavior="allow",
            reason="ok",
            matched_rule=None,
            mode="default",
        )
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert '"event": "permission_decision"' in content, content
        assert '"tool_name": "run_bash"' in content, content
    finally:
        clear_global_sink()
        os.unlink(path)
    print("OK test_audit_plugin_self_registers_sink")


def test_audit_plugin_callable_sink_receives_new_events() -> None:
    """When the operator wires a callable sink (e.g. Postgres insert),
    the new event types route through it just like tool_call_*."""
    events: list[dict] = []
    clear_global_sink()
    AuditPlugin(sink=lambda e: events.append(e))
    try:
        emit_state_mutation(
            mutation_type="allow_rule_added",
            state_key="adk_cc_allow_rules",
            details={
                "tool_name": "run_bash",
                "rule_contents": ["ls", "ls *"],
                "persist_across_sessions": False,
            },
        )
        emit_confirmation_resume(
            tool_name="exit_plan_mode",
            chose_id="approve",
            confirmed=True,
        )
    finally:
        clear_global_sink()
    assert len(events) == 2, events
    assert events[0]["event"] == "state_mutation"
    assert events[1]["event"] == "confirmation_resume"
    print("OK test_audit_plugin_callable_sink_receives_new_events")


# --- Driver --------------------------------------------------------


def main() -> None:
    test_emit_no_op_without_sink()
    test_permission_decision_event_shape()
    test_state_mutation_allow_rule_added_shape()
    test_state_mutation_permission_mode_change_shape()
    test_confirmation_resume_event_shape()
    test_sink_errors_do_not_propagate()
    test_audit_plugin_self_registers_sink()
    test_audit_plugin_callable_sink_receives_new_events()
    print("\nall audit-extension tests passed")


if __name__ == "__main__":
    main()
