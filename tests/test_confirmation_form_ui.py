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

  History filter (before_model_callback):
    - Wrapper function_call events (name=sentinel) are dropped.
    - User function_responses matching wrapper call_ids are dropped.
    - Intermediate `{status: "needs_confirmation"}` gate responses dropped.
    - History with no wrapper events is untouched.
    - The model's original function_call + final function_response survive.

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


def test_build_choice_schema_uses_one_boolean_property_per_option() -> None:
    """The bundled UI's form widget ignores `enum`; only `type` matters.
    A boolean property per option renders as one checkbox per choice."""
    schema = _build_choice_schema(_confirm_payload()["options"])
    assert schema is not None
    assert schema["type"] == "object"
    # Property keys are the option ids, in the same order.
    assert list(schema["properties"].keys()) == [
        "allow_once",
        "allow_always",
        "deny",
    ]
    for key in ("allow_once", "allow_always", "deny"):
        prop = schema["properties"][key]
        assert prop["type"] == "boolean", prop
        # description carries the human-readable label + option description
        assert prop["description"], prop
    # Sanity-check the specific descriptions wire through.
    assert "Allow once" in schema["properties"]["allow_once"]["description"]
    assert "Stop asking" in schema["properties"]["allow_always"]["description"]
    print("OK test_build_choice_schema_uses_one_boolean_property_per_option")


def test_build_choice_schema_empty_options_returns_none() -> None:
    assert _build_choice_schema([]) is None
    assert _build_choice_schema([{"label": "no id"}]) is None
    assert _build_choice_schema([{"id": 42}]) is None  # id must be str
    print("OK test_build_choice_schema_empty_options_returns_none")


def test_build_choice_schema_skips_reserved_keys() -> None:
    """An option whose id collides with a reserved key (`confirmed`,
    `choice`, `chose_id`, `result`) would confuse the inbound disambig,
    so the plugin drops it from the schema rather than risking shadowing."""
    options = [
        {"id": "allow", "label": "Allow", "description": ""},
        {"id": "chose_id", "label": "Bogus", "description": ""},  # reserved
        {"id": "deny", "label": "Deny", "description": ""},
    ]
    schema = _build_choice_schema(options)
    assert schema is not None
    assert set(schema["properties"].keys()) == {"allow", "deny"}
    print("OK test_build_choice_schema_skips_reserved_keys")


def test_extract_chose_id_accepts_all_shapes() -> None:
    # 1. Single-string shapes (legacy + payload-aware).
    assert _extract_chose_id({"choice": "allow_once"}) == "allow_once"
    assert _extract_chose_id({"chose_id": "deny"}) == "deny"
    # 2. Bundled UI free-form fallback.
    assert _extract_chose_id({"result": "allow_always"}) == "allow_always"
    # 3. Boolean-per-option (current bundled UI form).
    assert _extract_chose_id(
        {"allow_once": False, "allow_always": True, "deny": False}
    ) == "allow_always"
    # First true wins for ambiguous multi-true (deterministic on insertion).
    assert _extract_chose_id(
        {"allow_once": True, "allow_always": True}
    ) == "allow_once"
    # All false → no choice → None.
    assert _extract_chose_id(
        {"allow_once": False, "deny": False}
    ) is None
    # Priority: choice > chose_id > result > booleans.
    assert _extract_chose_id({"choice": "a", "deny": True}) == "a"
    # Garbage doesn't crash.
    assert _extract_chose_id(None) is None
    assert _extract_chose_id({}) is None
    assert _extract_chose_id({"choice": 42}) is None
    assert _extract_chose_id("not a dict") is None
    # `confirmed` is a reserved key — `confirmed: True` must NOT be
    # mistaken for a chose_id called "confirmed".
    assert _extract_chose_id({"confirmed": True}) is None
    print("OK test_extract_chose_id_accepts_all_shapes")


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
    # response_schema is now in args. One boolean property per option.
    assert "response_schema" in fc.args
    schema = fc.args["response_schema"]
    assert list(schema["properties"].keys()) == [
        "allow_once",
        "allow_always",
        "deny",
    ]
    for key in schema["properties"]:
        assert schema["properties"][key]["type"] == "boolean"
    # Prompt text is set so the bundled UI shows the title/detail above the form.
    assert "Confirm run_bash?" in fc.args.get("prompt", "")
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


def test_inbound_reshape_boolean_per_option_form_submission() -> None:
    """Current bundled UI form: user ticks one of N checkboxes; the
    submitted response is `{<chose_id_a>: false, <chose_id_b>: true, ...}`.
    The plugin reshapes to ADK's ToolConfirmation shape."""
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"allow_once": False, "allow_always": True, "deny": False},
    )
    _run_user_msg(plugin, msg)
    fr = _first_fr(msg)
    assert fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    assert fr.response == {"confirmed": True, "payload": {"chose_id": "allow_always"}}
    print("OK test_inbound_reshape_boolean_per_option_form_submission")


def test_inbound_boolean_form_deny_sets_confirmed_false() -> None:
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"allow_once": False, "allow_always": False, "deny": True},
    )
    _run_user_msg(plugin, msg)
    fr = _first_fr(msg)
    assert fr.response == {"confirmed": False, "payload": {"chose_id": "deny"}}
    print("OK test_inbound_boolean_form_deny_sets_confirmed_false")


def test_inbound_boolean_form_all_false_treated_as_no_choice() -> None:
    """User submitted the form without ticking anything. No mutation;
    let ADK's processor surface the error naturally."""
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"allow_once": False, "allow_always": False, "deny": False},
    )
    out = _run_user_msg(plugin, msg)
    assert out is None
    fr = _first_fr(msg)
    assert fr.name == CONFIRMATION_FORM_FUNCTION_CALL_NAME
    print("OK test_inbound_boolean_form_all_false_treated_as_no_choice")


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


# --- before_model_callback: filter wrapper bookkeeping from LLM history ---


class _FakeLlmRequest:
    """Minimal LlmRequest stand-in: only `contents` is consulted by the
    plugin's filter."""

    def __init__(self, contents: list) -> None:
        self.contents = contents


def _fn_call_content(role: str, name: str, call_id: str, args: Optional[dict] = None) -> types.Content:
    return types.Content(
        role=role,
        parts=[
            types.Part(
                function_call=types.FunctionCall(id=call_id, name=name, args=args or {})
            )
        ],
    )


def _fn_response_content(role: str, name: str, call_id: str, response: Any) -> types.Content:
    return types.Content(
        role=role,
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=call_id, name=name, response=response
                )
            )
        ],
    )


def _user_text(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


def _run_before_model(plugin: ConfirmationFormUiPlugin, req: _FakeLlmRequest):
    return asyncio.run(
        plugin.before_model_callback(callback_context=None, llm_request=req)
    )


def test_filter_drops_wrapper_call_response_and_gate() -> None:
    """Full deny-cycle history: model emits run_bash, gate fires,
    wrapper renamed by on_event_callback, user denies, resume re-runs
    run_bash → final response. The filter strips:
      - wrapper function_call
      - user's function_response for wrapper id
      - intermediate `needs_confirmation` gate response
    Leaving: user text + assistant run_bash + final tool response."""
    plugin = ConfirmationFormUiPlugin()
    req = _FakeLlmRequest([
        _user_text("please rm /tmp/foo"),
        _fn_call_content("model", "run_bash", "orig-1", {"command": "rm /tmp/foo"}),
        _fn_response_content(
            "user", "run_bash", "orig-1", {"status": "needs_confirmation"}
        ),
        _fn_call_content(
            "user", CONFIRMATION_FORM_FUNCTION_CALL_NAME, "wrap-1",
            {"originalFunctionCall": {"id": "orig-1", "name": "run_bash"}},
        ),
        _fn_response_content(
            "user", "adk_request_confirmation", "wrap-1",
            {"confirmed": False, "payload": {"chose_id": "deny"}},
        ),
        _fn_response_content(
            "user", "run_bash", "orig-1",
            {"status": "permission_denied_by_user"},
        ),
    ])

    _run_before_model(plugin, req)

    # Survivors: user text, assistant run_bash, final tool response.
    survivors: list[tuple[str, str]] = []
    for c in req.contents:
        for p in c.parts:
            if p.text:
                survivors.append((c.role, f"text:{p.text}"))
            elif p.function_call:
                survivors.append((c.role, f"call:{p.function_call.name}#{p.function_call.id}"))
            elif p.function_response:
                survivors.append((
                    c.role,
                    f"resp:{p.function_response.name}#{p.function_response.id}",
                ))
    assert survivors == [
        ("user", "text:please rm /tmp/foo"),
        ("model", "call:run_bash#orig-1"),
        ("user", "resp:run_bash#orig-1"),
    ], survivors

    # The final tool response is the deny status (not "needs_confirmation").
    final = req.contents[-1].parts[0].function_response.response
    assert final == {"status": "permission_denied_by_user"}, final
    print("OK test_filter_drops_wrapper_call_response_and_gate")


def test_filter_noop_when_no_wrapper_present() -> None:
    """Conversations that don't use the form bridge are untouched."""
    plugin = ConfirmationFormUiPlugin()
    original = [
        _user_text("hi"),
        _fn_call_content("model", "read_file", "c1", {"path": "/etc/hosts"}),
        _fn_response_content("user", "read_file", "c1", {"content": "..."}),
    ]
    req = _FakeLlmRequest(list(original))
    _run_before_model(plugin, req)
    # contents reference might be different (no-op returns early without
    # rebuilding), so compare by structure: same length, same calls/responses.
    assert len(req.contents) == 3
    assert req.contents[1].parts[0].function_call.name == "read_file"
    assert req.contents[2].parts[0].function_response.id == "c1"
    print("OK test_filter_noop_when_no_wrapper_present")


def test_filter_preserves_user_text_in_wrapper_round_trip() -> None:
    """Even when the user's resume submission is mixed with text, the
    filter only drops the function_response part — surrounding text
    survives. (In practice the bundled UI submits function_response
    alone, but the filter is defensive.)"""
    plugin = ConfirmationFormUiPlugin()
    mixed = types.Content(
        role="user",
        parts=[
            types.Part(text="here is my answer"),
            types.Part(
                function_response=types.FunctionResponse(
                    id="wrap-1",
                    name="adk_request_confirmation",
                    response={"confirmed": False, "payload": {"chose_id": "deny"}},
                )
            ),
        ],
    )
    req = _FakeLlmRequest([
        _fn_call_content("user", CONFIRMATION_FORM_FUNCTION_CALL_NAME, "wrap-1"),
        mixed,
    ])
    _run_before_model(plugin, req)
    # Wrapper call dropped; user content kept but its function_response
    # part filtered → leaves only the text part.
    assert len(req.contents) == 1
    kept_parts = req.contents[0].parts
    assert len(kept_parts) == 1
    assert kept_parts[0].text == "here is my answer"
    print("OK test_filter_preserves_user_text_in_wrapper_round_trip")


def test_filter_drops_only_matching_gate_responses() -> None:
    """`needs_confirmation` is the marker; other function_responses with
    a `status` field (e.g. `ok`) are NOT filtered."""
    plugin = ConfirmationFormUiPlugin()
    req = _FakeLlmRequest([
        _fn_call_content("user", CONFIRMATION_FORM_FUNCTION_CALL_NAME, "wrap-1"),
        _fn_response_content("user", "run_bash", "orig-1", {"status": "ok", "stdout": "..."}),
        _fn_response_content("user", "run_bash", "orig-2", {"status": "needs_confirmation"}),
    ])
    _run_before_model(plugin, req)
    # Only the needs_confirmation response is filtered (alongside the wrapper).
    surviving_responses = [
        p.function_response
        for c in req.contents
        for p in c.parts
        if p.function_response
    ]
    assert len(surviving_responses) == 1
    assert surviving_responses[0].id == "orig-1"
    print("OK test_filter_drops_only_matching_gate_responses")


def test_filter_handles_empty_or_missing_contents() -> None:
    plugin = ConfirmationFormUiPlugin()
    # Empty contents → no-op, no crash.
    req = _FakeLlmRequest([])
    _run_before_model(plugin, req)
    assert req.contents == []
    # `contents` attribute missing → no-op, no crash.
    class _NoContents:
        pass
    out = _run_before_model(plugin, _NoContents())
    assert out is None
    print("OK test_filter_handles_empty_or_missing_contents")


# --- Driver ---------------------------------------------------------


def main() -> None:
    test_build_choice_schema_uses_one_boolean_property_per_option()
    test_build_choice_schema_empty_options_returns_none()
    test_build_choice_schema_skips_reserved_keys()
    test_extract_chose_id_accepts_all_shapes()
    test_outbound_renames_and_injects_response_schema()
    test_outbound_skips_event_without_options()
    test_outbound_ignores_non_confirmation_events()
    test_outbound_handles_empty_content()
    test_inbound_reshape_allow_once_choice()
    test_inbound_reshape_deny_sets_confirmed_false()
    test_inbound_accepts_legacy_chose_id_shape()
    test_inbound_accepts_free_form_result_fallback()
    test_inbound_garbage_response_left_unchanged()
    test_inbound_reshape_boolean_per_option_form_submission()
    test_inbound_boolean_form_deny_sets_confirmed_false()
    test_inbound_boolean_form_all_false_treated_as_no_choice()
    test_inbound_ignores_non_matching_names()
    test_inbound_handles_empty_parts()
    test_filter_drops_wrapper_call_response_and_gate()
    test_filter_noop_when_no_wrapper_present()
    test_filter_preserves_user_text_in_wrapper_round_trip()
    test_filter_drops_only_matching_gate_responses()
    test_filter_handles_empty_or_missing_contents()
    print("\nall confirmation_form_ui tests passed")


if __name__ == "__main__":
    main()
