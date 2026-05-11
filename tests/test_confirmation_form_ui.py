"""Unit tests for ConfirmationFormUiPlugin.

Covers both ends of the bidirectional rewrite:

  Outbound (on_event_callback):
    - adk_request_confirmation event with a ConfirmPrompt payload →
      rewritten: name swapped to the form sentinel, response_schema
      injected, function_call id preserved.
    - Confirmation event with empty/missing payload options → not
      rewritten (left as-is so the binary widget still works).
    - Non-confirmation events → not touched.
    - Event with no content/parts → no-op (no crash).

  Inbound (on_user_message_callback):
    - User submission for the sentinel name with `{choice: "<id>"}` →
      reshaped to `{confirmed: bool, payload: {chose_id: "<id>"}}` and
      name swapped back to adk_request_confirmation.
    - User submission with the legacy `{chose_id: "<id>"}` shape →
      same reshape.
    - User submission with `{result: "<text>"}` (free-form fallback) →
      best-effort: treat the text as a chose_id.
    - Garbage response → not rewritten (let ADK surface the error).
    - "deny" chose_id → confirmed=False; everything else → confirmed=True.
    - Non-matching name → left untouched.

Run: `.venv/bin/python tests/test_confirmation_form_ui.py`
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from google.adk.events.event import Event
from google.adk.flows.llm_flows.functions import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
from google.genai import types

from adk_cc.plugins.confirmation_form_ui import (
    CONFIRMATION_FORM_FUNCTION_CALL_NAME,
    ConfirmationFormUiPlugin,
    _build_choice_schema,
    _extract_chose_id,
)


# --- Helpers --------------------------------------------------------


def _confirm_payload() -> dict:
    """Canonical ConfirmPrompt as produced by allow_once_always_deny_prompt."""
    return {
        "style": "single_select",
        "title": "Confirm run_bash?",
        "detail": "destructive run_bash requires confirmation",
        "options": [
            {"id": "allow_once", "label": "Allow once", "description": "Run this one time."},
            {"id": "allow_always", "label": "Allow always", "description": "Stop asking."},
            {"id": "deny", "label": "Deny", "description": "Cancel."},
        ],
    }


def _confirmation_event(
    payload: Optional[dict] = None,
    *,
    call_id: str = "wrap-1",
    name: str = REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
) -> Event:
    args: dict[str, Any] = {
        "originalFunctionCall": {"id": "orig-1", "name": "run_bash", "args": {"command": "rm /tmp/foo"}},
    }
    if payload is not None:
        args["toolConfirmation"] = {
            "hint": "destructive run_bash requires confirmation",
            "confirmed": False,
            "payload": payload,
        }
    return Event(
        invocation_id="inv-1",
        author="test-agent",
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args))],
        ),
    )


def _user_message(name: str, response: dict, *, call_id: str = "wrap-1") -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=call_id, name=name, response=response
                )
            )
        ],
    )


def _run_event(plugin: ConfirmationFormUiPlugin, event: Event) -> Optional[Event]:
    return asyncio.run(
        plugin.on_event_callback(invocation_context=None, event=event)
    )


def _run_user_msg(
    plugin: ConfirmationFormUiPlugin, msg: types.Content
) -> Optional[types.Content]:
    return asyncio.run(
        plugin.on_user_message_callback(invocation_context=None, user_message=msg)
    )


def _first_fc(event: Event) -> types.FunctionCall:
    return event.content.parts[0].function_call


def _first_fr(msg: types.Content) -> types.FunctionResponse:
    return msg.parts[0].function_response


# --- Schema-builder unit cases --------------------------------------


def test_build_choice_schema_includes_all_option_ids_in_enum() -> None:
    schema = _build_choice_schema(_confirm_payload()["options"], title="Confirm run_bash?")
    assert schema is not None
    assert schema["type"] == "object"
    assert "choice" in schema["properties"]
    choice = schema["properties"]["choice"]
    assert choice["type"] == "string"
    assert choice["enum"] == ["allow_once", "allow_always", "deny"]
    # Description carries the title + per-option labels for operator clarity.
    desc = choice["description"]
    assert "Confirm run_bash?" in desc
    for token in ("allow_once", "allow_always", "deny", "Allow once", "Deny"):
        assert token in desc, f"missing {token!r} in description"
    print("OK test_build_choice_schema_includes_all_option_ids_in_enum")


def test_build_choice_schema_empty_options_returns_none() -> None:
    assert _build_choice_schema([], None) is None
    assert _build_choice_schema([{"label": "no id"}], None) is None
    assert _build_choice_schema([{"id": 42}], None) is None  # id must be str
    print("OK test_build_choice_schema_empty_options_returns_none")


def test_extract_chose_id_accepts_both_shapes() -> None:
    assert _extract_chose_id({"choice": "allow_once"}) == "allow_once"
    assert _extract_chose_id({"chose_id": "deny"}) == "deny"
    # `result` is the bundled UI's free-form fallback shape.
    assert _extract_chose_id({"result": "allow_always"}) == "allow_always"
    # Priority: choice > chose_id > result.
    assert _extract_chose_id({"choice": "a", "chose_id": "b"}) == "a"
    # Garbage doesn't crash.
    assert _extract_chose_id(None) is None
    assert _extract_chose_id({}) is None
    assert _extract_chose_id({"choice": 42}) is None
    assert _extract_chose_id("not a dict") is None
    print("OK test_extract_chose_id_accepts_both_shapes")


# --- Outbound rewrite -----------------------------------------------


def test_outbound_renames_and_injects_response_schema() -> None:
    plugin = ConfirmationFormUiPlugin()
    event = _confirmation_event(_confirm_payload())
    out = _run_event(plugin, event)

    # A mutated event was returned (non-None).
    assert out is event
    fc = _first_fc(event)
    # Name swapped to the sentinel so the bundled UI's binary-widget
    # short-circuit doesn't trigger.
    assert fc.name == CONFIRMATION_FORM_FUNCTION_CALL_NAME
    # ID preserved — ADK's resume processor matches on id, not name.
    assert fc.id == "wrap-1"
    # response_schema is now in args.
    assert "response_schema" in fc.args
    schema = fc.args["response_schema"]
    assert schema["properties"]["choice"]["enum"] == [
        "allow_once",
        "allow_always",
        "deny",
    ]
    # Original toolConfirmation is preserved so payload-aware frontends
    # can still read it.
    assert fc.args["toolConfirmation"]["payload"]["style"] == "single_select"
    # originalFunctionCall preserved for ADK's resume.
    assert fc.args["originalFunctionCall"]["name"] == "run_bash"
    print("OK test_outbound_renames_and_injects_response_schema")


def test_outbound_skips_event_without_options() -> None:
    """Confirmation event with a payload but no options array — leave it
    alone so the bundled UI's binary widget still works for non-multi
    confirmations (or for legacy callers that don't set options)."""
    plugin = ConfirmationFormUiPlugin()
    # Payload exists but has no options.
    event = _confirmation_event({"style": "confirm_deny", "title": "x", "detail": "y", "options": []})
    out = _run_event(plugin, event)
    assert out is None
    fc = _first_fc(event)
    assert fc.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    assert "response_schema" not in (fc.args or {})
    print("OK test_outbound_skips_event_without_options")


def test_outbound_ignores_non_confirmation_events() -> None:
    plugin = ConfirmationFormUiPlugin()
    # A regular tool call, not a confirmation wrapper.
    event = Event(
        invocation_id="inv-1",
        author="test-agent",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id="c1", name="run_bash", args={"command": "ls"}
                    )
                )
            ],
        ),
    )
    out = _run_event(plugin, event)
    assert out is None
    fc = _first_fc(event)
    assert fc.name == "run_bash"
    print("OK test_outbound_ignores_non_confirmation_events")


def test_outbound_handles_empty_content() -> None:
    plugin = ConfirmationFormUiPlugin()
    empty = Event(invocation_id="inv-1", author="test-agent", content=None)
    out = _run_event(plugin, empty)
    assert out is None
    print("OK test_outbound_handles_empty_content")


# --- Inbound rewrite ------------------------------------------------


def test_inbound_reshape_allow_once_choice() -> None:
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"choice": "allow_once"})
    out = _run_user_msg(plugin, msg)
    assert out is msg
    fr = _first_fr(msg)
    assert fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    assert fr.response == {"confirmed": True, "payload": {"chose_id": "allow_once"}}
    print("OK test_inbound_reshape_allow_once_choice")


def test_inbound_reshape_deny_sets_confirmed_false() -> None:
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"choice": "deny"})
    _run_user_msg(plugin, msg)
    fr = _first_fr(msg)
    assert fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    assert fr.response == {"confirmed": False, "payload": {"chose_id": "deny"}}
    print("OK test_inbound_reshape_deny_sets_confirmed_false")


def test_inbound_accepts_legacy_chose_id_shape() -> None:
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"chose_id": "allow_always"})
    _run_user_msg(plugin, msg)
    fr = _first_fr(msg)
    assert fr.response == {"confirmed": True, "payload": {"chose_id": "allow_always"}}
    print("OK test_inbound_accepts_legacy_chose_id_shape")


def test_inbound_accepts_free_form_result_fallback() -> None:
    """Bundled UI falls back to {result: <text>} when no response_schema
    matched. If the operator typed a valid chose_id, recover it."""
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"result": "allow_once"})
    _run_user_msg(plugin, msg)
    fr = _first_fr(msg)
    assert fr.response == {"confirmed": True, "payload": {"chose_id": "allow_once"}}
    print("OK test_inbound_accepts_free_form_result_fallback")


def test_inbound_garbage_response_left_unchanged() -> None:
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"unrelated": "blob"})
    out = _run_user_msg(plugin, msg)
    # No mutation happened, so the callback returns None and the original
    # message goes through (and ADK's processor will fail to recognize it,
    # which is fine — better to surface than silently swallow).
    assert out is None
    fr = _first_fr(msg)
    assert fr.name == CONFIRMATION_FORM_FUNCTION_CALL_NAME, "name not renamed back"
    print("OK test_inbound_garbage_response_left_unchanged")


def test_inbound_ignores_non_matching_names() -> None:
    plugin = ConfirmationFormUiPlugin()
    # Could be a regular tool response, or the legacy confirmation
    # response. Either way, this plugin doesn't touch it.
    msg = _user_message("run_bash", {"stdout": "..."})
    out = _run_user_msg(plugin, msg)
    assert out is None
    fr = _first_fr(msg)
    assert fr.name == "run_bash"
    print("OK test_inbound_ignores_non_matching_names")


def test_inbound_handles_empty_parts() -> None:
    plugin = ConfirmationFormUiPlugin()
    empty = types.Content(role="user", parts=[])
    out = _run_user_msg(plugin, empty)
    assert out is None
    print("OK test_inbound_handles_empty_parts")


# --- Driver ---------------------------------------------------------


def main() -> None:
    test_build_choice_schema_includes_all_option_ids_in_enum()
    test_build_choice_schema_empty_options_returns_none()
    test_extract_chose_id_accepts_both_shapes()
    test_outbound_renames_and_injects_response_schema()
    test_outbound_skips_event_without_options()
    test_outbound_ignores_non_confirmation_events()
    test_outbound_handles_empty_content()
    test_inbound_reshape_allow_once_choice()
    test_inbound_reshape_deny_sets_confirmed_false()
    test_inbound_accepts_legacy_chose_id_shape()
    test_inbound_accepts_free_form_result_fallback()
    test_inbound_garbage_response_left_unchanged()
    test_inbound_ignores_non_matching_names()
    test_inbound_handles_empty_parts()
    print("\nall confirmation_form_ui tests passed")


if __name__ == "__main__":
    main()
