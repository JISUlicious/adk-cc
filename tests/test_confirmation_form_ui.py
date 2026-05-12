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
    _extract_comment,
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


class _FakeSession:
    def __init__(self, events: Optional[list] = None) -> None:
        self.events = list(events or [])


class _FakeInvocationCtx:
    def __init__(self, events: Optional[list] = None) -> None:
        self.session = _FakeSession(events)


def _wrap_call_event(wrap_id: str, *, payload: Optional[dict] = None) -> Event:
    """A function-call event with our sentinel name — what
    `on_event_callback` produces after renaming
    `adk_request_confirmation`. Represents an outstanding wrap.

    When `payload` is supplied, it's attached as
    `toolConfirmation.payload` so the inbound side can look up the
    options list (needed for the binary-collapse fallback to know
    which id is the negative)."""
    args: dict[str, Any] = {"originalFunctionCall": {"id": f"orig-{wrap_id}"}}
    if payload is not None:
        args["toolConfirmation"] = {
            "hint": "test",
            "confirmed": False,
            "payload": payload,
        }
    return Event(
        invocation_id="inv-1",
        author="test-agent",
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id=wrap_id,
                        name=CONFIRMATION_FORM_FUNCTION_CALL_NAME,
                        args=args,
                    )
                )
            ],
        ),
    )


def _binary_payload() -> dict:
    """Canonical 2-option `ConfirmPrompt` (approve/deny) as
    `exit_plan_mode` would send. Used by binary-collapse tests."""
    return {
        "style": "single_select",
        "title": "Exit plan mode?",
        "detail": "rewrite the auth middleware",
        "options": [
            {"id": "approve", "label": "Approve", "description": "Go."},
            {"id": "deny", "label": "Deny", "description": "Hold."},
        ],
        "with_comment": True,
    }


def _user_response_event(wrap_id: str, name: str, response: dict) -> Event:
    """A user-authored function_response event — used to seed the
    session with deferred (`PENDING_CONFIRMATION_NAME`) or resolved
    (`adk_request_confirmation`) submissions."""
    return Event(
        invocation_id="inv-1",
        author="user",
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=wrap_id, name=name, response=response
                    )
                )
            ],
        ),
    )


def _run_user_msg(
    plugin: ConfirmationFormUiPlugin,
    msg: types.Content,
    *,
    invocation_context: Optional[Any] = None,
) -> Optional[types.Content]:
    return asyncio.run(
        plugin.on_user_message_callback(
            invocation_context=invocation_context, user_message=msg
        )
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
    so the plugin drops it from the schema rather than risking shadowing.
    Here that filtering leaves 2 valid options, which then hit the
    binary collapse → a single positive boolean keyed on "allow"."""
    options = [
        {"id": "allow", "label": "Allow", "description": ""},
        {"id": "chose_id", "label": "Bogus", "description": ""},  # reserved → dropped
        {"id": "deny", "label": "Deny", "description": ""},
    ]
    schema = _build_choice_schema(options)
    assert schema is not None
    # Reserved id dropped; remaining 2 collapse → only positive id "allow".
    assert set(schema["properties"].keys()) == {"allow"}
    print("OK test_build_choice_schema_skips_reserved_keys")


def test_build_choice_schema_with_comment_adds_textbox() -> None:
    """`with_comment=True` adds a `comment` string property so the
    bundled UI renders an optional free-form textbox alongside the
    option checkbox(es). For a binary prompt (2 options), the schema
    collapses to a single positive boolean — so the comment textbox
    sits beside ONE checkbox, not two."""
    from adk_cc.plugins.confirmation_form_ui import COMMENT_FIELD_KEY

    options = [
        {"id": "approve", "label": "Approve", "description": "Go ahead"},
        {"id": "deny", "label": "Deny", "description": "Hold up"},
    ]
    schema = _build_choice_schema(options, with_comment=True)
    assert schema is not None
    # Binary collapse: only the positive option's boolean + comment.
    assert set(schema["properties"].keys()) == {"approve", COMMENT_FIELD_KEY}
    comment_prop = schema["properties"][COMMENT_FIELD_KEY]
    assert comment_prop["type"] == "string"
    # Description hints at the dual use (approve + comment / deny + comment).
    assert "Optional" in comment_prop["description"]
    print("OK test_build_choice_schema_with_comment_adds_textbox")


def test_build_choice_schema_binary_collapses_to_single_boolean() -> None:
    """2-option prompts render as ONE checkbox keyed on the positive
    option. The operator physically can't pick both — there's only one
    box. The description explains the unchecked meaning ("Unchecked =
    Deny.") so the empty state isn't ambiguous."""
    options = [
        {"id": "approve", "label": "Approve", "description": "Go ahead"},
        {"id": "deny", "label": "Deny", "description": "Hold up"},
    ]
    schema = _build_choice_schema(options)
    assert schema is not None
    assert list(schema["properties"].keys()) == ["approve"], schema
    approve_prop = schema["properties"]["approve"]
    assert approve_prop["type"] == "boolean"
    # Description teaches the operator what unchecked means.
    assert "Approve" in approve_prop["description"]
    assert "Deny" in approve_prop["description"]
    assert "unchecked" in approve_prop["description"].lower()
    print("OK test_build_choice_schema_binary_collapses_to_single_boolean")


def test_build_choice_schema_binary_no_deny_id_uses_first_as_positive() -> None:
    """When neither id is `"deny"`, the FIRST option is the positive
    (matches the codebase's positive-then-negative ordering)."""
    options = [
        {"id": "yes", "label": "Yes", "description": "Proceed"},
        {"id": "no", "label": "No", "description": "Cancel"},
    ]
    schema = _build_choice_schema(options)
    assert list(schema["properties"].keys()) == ["yes"]
    assert "Yes" in schema["properties"]["yes"]["description"]
    assert "No" in schema["properties"]["yes"]["description"]
    print("OK test_build_choice_schema_binary_no_deny_id_uses_first_as_positive")


def test_build_choice_schema_binary_deny_first_picks_second_as_positive() -> None:
    """If somehow deny is the FIRST option, the second one is treated
    as positive — defensive against future callers ordering options
    differently."""
    options = [
        {"id": "deny", "label": "Deny", "description": "Cancel"},
        {"id": "allow", "label": "Allow", "description": "Run it"},
    ]
    schema = _build_choice_schema(options)
    assert list(schema["properties"].keys()) == ["allow"]
    print("OK test_build_choice_schema_binary_deny_first_picks_second_as_positive")


def test_build_choice_schema_without_comment_no_textbox() -> None:
    """Default `with_comment=False` produces no comment property — the
    destructive-tool gate doesn't need it."""
    options = [{"id": "allow", "label": "Allow", "description": ""}]
    schema = _build_choice_schema(options)
    assert "comment" not in schema["properties"]
    print("OK test_build_choice_schema_without_comment_no_textbox")


def test_extract_comment_helper() -> None:
    """Returns stripped string when present + non-empty; None otherwise."""
    assert _extract_comment({"comment": "hello"}) == "hello"
    assert _extract_comment({"comment": "  trim me  "}) == "trim me"
    # Bundled UI sends null for empty textboxes after getCleanedFormModel.
    assert _extract_comment({"comment": None}) is None
    assert _extract_comment({"comment": ""}) is None
    assert _extract_comment({"comment": "    "}) is None
    assert _extract_comment({}) is None
    assert _extract_comment(None) is None
    assert _extract_comment("not a dict") is None
    print("OK test_extract_comment_helper")


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
    # Deny wins on multi-true (safety) — overrides iteration-order.
    assert _extract_chose_id(
        {"allow_once": True, "allow_always": True, "deny": True}
    ) == "deny"
    # Non-deny multi-true: first true wins, deterministic on insertion.
    assert _extract_chose_id(
        {"allow_once": True, "allow_always": True}
    ) == "allow_once"
    # All false, no options → no choice → None.
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


def test_extract_chose_id_binary_unchecked_returns_negative_id() -> None:
    """When `options` describes a 2-option prompt AND the response has
    no True boolean, the function infers the operator left the lone
    positive checkbox unchecked → returns the negative id."""
    binary_options = [
        {"id": "approve", "label": "Approve", "description": ""},
        {"id": "deny", "label": "Deny", "description": ""},
    ]
    # No True booleans at all (operator submitted with the box unchecked).
    assert (
        _extract_chose_id({"approve": False}, options=binary_options) == "deny"
    )
    # Empty response (defensive — bundled UI submits {} when nothing was filled).
    assert _extract_chose_id({}, options=binary_options) == "deny"
    # Even with a comment but no chosen box, unchecked → deny.
    assert (
        _extract_chose_id(
            {"approve": False, "comment": "no thanks"}, options=binary_options
        )
        == "deny"
    )
    # When the positive IS checked, the binary fallback does NOT fire —
    # the True branch wins.
    assert (
        _extract_chose_id({"approve": True}, options=binary_options) == "approve"
    )
    print("OK test_extract_chose_id_binary_unchecked_returns_negative_id")


def test_extract_chose_id_binary_no_deny_id_uses_second_as_negative() -> None:
    """Symmetric to the schema-builder: when neither option's id is
    "deny", the SECOND option is the negative. Unchecked → that id."""
    binary_options = [
        {"id": "yes", "label": "Yes", "description": ""},
        {"id": "no", "label": "No", "description": ""},
    ]
    assert _extract_chose_id({"yes": False}, options=binary_options) == "no"
    assert _extract_chose_id({}, options=binary_options) == "no"
    print("OK test_extract_chose_id_binary_no_deny_id_uses_second_as_negative")


def test_extract_chose_id_three_options_unchecked_still_returns_none() -> None:
    """For N≥3 prompts, all-False does NOT auto-deny — operator
    explicitly didn't pick anything, so the response is ambiguous and
    should surface as such (None → deferred / unparseable)."""
    triple = [
        {"id": "allow_once", "label": "", "description": ""},
        {"id": "allow_always", "label": "", "description": ""},
        {"id": "deny", "label": "", "description": ""},
    ]
    assert (
        _extract_chose_id(
            {"allow_once": False, "allow_always": False, "deny": False},
            options=triple,
        )
        is None
    )
    print("OK test_extract_chose_id_three_options_unchecked_still_returns_none")


# --- Outbound rewrite -----------------------------------------------


def test_outbound_with_comment_flag_adds_textbox_property() -> None:
    """A ConfirmPrompt carrying `with_comment=True` gets a `comment`
    string property in the schema — bundled UI then renders a textbox
    alongside the option checkboxes."""
    from adk_cc.plugins.confirmation_form_ui import COMMENT_FIELD_KEY

    plugin = ConfirmationFormUiPlugin()
    payload = _confirm_payload() | {"with_comment": True}
    event = _confirmation_event(payload)
    _run_event(plugin, event)
    fc = _first_fc(event)
    schema = fc.args["response_schema"]
    assert COMMENT_FIELD_KEY in schema["properties"]
    assert schema["properties"][COMMENT_FIELD_KEY]["type"] == "string"
    print("OK test_outbound_with_comment_flag_adds_textbox_property")


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


def _ctx_with_one_outstanding(wrap_id: str = "wrap-1") -> _FakeInvocationCtx:
    """One outstanding wrap; a single submission completes the set and
    triggers the bundle path."""
    return _FakeInvocationCtx([_wrap_call_event(wrap_id)])


def test_inbound_reshape_allow_once_choice() -> None:
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"choice": "allow_once"})
    out = _run_user_msg(plugin, msg, invocation_context=_ctx_with_one_outstanding())
    assert out is not None
    fr = out.parts[0].function_response
    assert fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    assert fr.response == {"confirmed": True, "payload": {"chose_id": "allow_once"}}
    print("OK test_inbound_reshape_allow_once_choice")


def test_inbound_reshape_deny_sets_confirmed_false() -> None:
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"choice": "deny"})
    out = _run_user_msg(plugin, msg, invocation_context=_ctx_with_one_outstanding())
    fr = out.parts[0].function_response
    assert fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    assert fr.response == {"confirmed": False, "payload": {"chose_id": "deny"}}
    print("OK test_inbound_reshape_deny_sets_confirmed_false")


def test_inbound_accepts_legacy_chose_id_shape() -> None:
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"chose_id": "allow_always"})
    out = _run_user_msg(plugin, msg, invocation_context=_ctx_with_one_outstanding())
    fr = out.parts[0].function_response
    assert fr.response == {"confirmed": True, "payload": {"chose_id": "allow_always"}}
    print("OK test_inbound_accepts_legacy_chose_id_shape")


def test_inbound_accepts_free_form_result_fallback() -> None:
    """Bundled UI falls back to {result: <text>} when no response_schema
    matched. If the operator typed a valid chose_id, recover it."""
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"result": "allow_once"})
    out = _run_user_msg(plugin, msg, invocation_context=_ctx_with_one_outstanding())
    fr = out.parts[0].function_response
    assert fr.response == {"confirmed": True, "payload": {"chose_id": "allow_once"}}
    print("OK test_inbound_accepts_free_form_result_fallback")


def test_inbound_garbage_response_left_unchanged() -> None:
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"unrelated": "blob"})
    out = _run_user_msg(plugin, msg, invocation_context=_ctx_with_one_outstanding())
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
    out = _run_user_msg(plugin, msg, invocation_context=_ctx_with_one_outstanding())
    fr = out.parts[0].function_response
    assert fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    assert fr.response == {"confirmed": True, "payload": {"chose_id": "allow_always"}}
    print("OK test_inbound_reshape_boolean_per_option_form_submission")


def test_inbound_comment_folds_into_payload() -> None:
    """When the form has a `comment` field and the operator typed
    something, it ends up in `toolConfirmation.payload.comment` —
    accessible via `_extract_user_comment` in the tool layer."""
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"approve": False, "deny": True, "comment": "try smaller scope first"},
    )
    out = _run_user_msg(plugin, msg, invocation_context=_ctx_with_one_outstanding())
    fr = out.parts[0].function_response
    assert fr.response == {
        "confirmed": False,
        "payload": {"chose_id": "deny", "comment": "try smaller scope first"},
    }
    print("OK test_inbound_comment_folds_into_payload")


def test_inbound_empty_comment_not_included() -> None:
    """Bundled UI sends null for empty textboxes (per its
    getCleanedFormModel). The reshape should omit the `comment` key
    entirely when there's no meaningful text — avoid sending the model
    `payload: {chose_id: ..., comment: ""}`-style noise."""
    plugin = ConfirmationFormUiPlugin()
    # Null / empty / whitespace-only — all treated as "no comment".
    for empty_value in (None, "", "   ", "\n  \t  "):
        msg = _user_message(
            CONFIRMATION_FORM_FUNCTION_CALL_NAME,
            {"approve": True, "deny": False, "comment": empty_value},
        )
        out = _run_user_msg(plugin, msg, invocation_context=_ctx_with_one_outstanding())
        fr = out.parts[0].function_response
        assert "comment" not in fr.response["payload"], (
            f"expected no comment key for {empty_value!r}, got {fr.response}"
        )
    print("OK test_inbound_empty_comment_not_included")


def test_inbound_comment_with_approve_passes_through() -> None:
    """Operator can add a comment on approve too (e.g. 'go ahead but
    be careful about X'). The model sees both signals."""
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"approve": True, "deny": False, "comment": "watch the edge case"},
    )
    out = _run_user_msg(plugin, msg, invocation_context=_ctx_with_one_outstanding())
    fr = out.parts[0].function_response
    assert fr.response == {
        "confirmed": True,
        "payload": {"chose_id": "approve", "comment": "watch the edge case"},
    }
    print("OK test_inbound_comment_with_approve_passes_through")


def test_inbound_binary_unchecked_form_resolves_to_deny() -> None:
    """End-to-end of the binary collapse:
      - Outbound schema was a single boolean keyed on `approve`.
      - Operator submitted with the box unchecked → `{approve: False}`.
      - Inbound resolves via the wrap event's payload → chose_id="deny",
        confirmed=False.
    """
    plugin = ConfirmationFormUiPlugin()
    ctx = _FakeInvocationCtx([_wrap_call_event("wrap-1", payload=_binary_payload())])
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"approve": False}
    )
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    fr = out.parts[0].function_response
    assert fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    assert fr.response == {"confirmed": False, "payload": {"chose_id": "deny"}}
    print("OK test_inbound_binary_unchecked_form_resolves_to_deny")


def test_inbound_binary_unchecked_with_comment_attaches_comment_to_deny() -> None:
    """Operator unchecks the lone box AND types a reason — the comment
    rides on the denied response so the model can revise the plan."""
    plugin = ConfirmationFormUiPlugin()
    ctx = _FakeInvocationCtx([_wrap_call_event("wrap-1", payload=_binary_payload())])
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"approve": False, "comment": "split into two phases"},
    )
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    fr = out.parts[0].function_response
    assert fr.response == {
        "confirmed": False,
        "payload": {"chose_id": "deny", "comment": "split into two phases"},
    }
    print("OK test_inbound_binary_unchecked_with_comment_attaches_comment_to_deny")


def test_inbound_binary_checked_form_resolves_to_approve() -> None:
    """Operator ticks the lone box → chose_id="approve", confirmed=True."""
    plugin = ConfirmationFormUiPlugin()
    ctx = _FakeInvocationCtx([_wrap_call_event("wrap-1", payload=_binary_payload())])
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME, {"approve": True}
    )
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    fr = out.parts[0].function_response
    assert fr.response == {"confirmed": True, "payload": {"chose_id": "approve"}}
    print("OK test_inbound_binary_checked_form_resolves_to_approve")


def test_inbound_three_option_deny_wins_over_multi_check() -> None:
    """If a (defensive / buggy) submission has `deny: True` alongside
    other True booleans, deny short-circuits — destructive ops fail
    closed instead of running because the operator also clicked
    `allow_once`."""
    plugin = ConfirmationFormUiPlugin()
    ctx = _ctx_with_one_outstanding()
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"allow_once": True, "allow_always": False, "deny": True},
    )
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    fr = out.parts[0].function_response
    assert fr.response == {"confirmed": False, "payload": {"chose_id": "deny"}}
    print("OK test_inbound_three_option_deny_wins_over_multi_check")


def test_inbound_boolean_form_deny_sets_confirmed_false() -> None:
    plugin = ConfirmationFormUiPlugin()
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"allow_once": False, "allow_always": False, "deny": True},
    )
    out = _run_user_msg(plugin, msg, invocation_context=_ctx_with_one_outstanding())
    fr = out.parts[0].function_response
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
    out = _run_user_msg(plugin, msg, invocation_context=_ctx_with_one_outstanding())
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


# --- Deferred-batch processing -------------------------------------


def test_first_of_three_defers_with_pending_name() -> None:
    """3 outstanding wraps, operator submits widget 1. Plugin defers:
    response renamed to the pending sentinel; no bundle yet."""
    plugin = ConfirmationFormUiPlugin()
    ctx = _FakeInvocationCtx([
        _wrap_call_event("w1"),
        _wrap_call_event("w2"),
        _wrap_call_event("w3"),
    ])
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"allow_once": True, "allow_always": False, "deny": False},
        call_id="w1",
    )
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    assert out is not None
    assert len(out.parts) == 1
    fr = out.parts[0].function_response
    # Deferred: renamed to pending sentinel so ADK's processor ignores it.
    from adk_cc.plugins.confirmation_form_ui import PENDING_CONFIRMATION_NAME
    assert fr.name == PENDING_CONFIRMATION_NAME
    assert fr.id == "w1"
    # The reshaped response IS persisted so the bundle can pick it up later.
    assert fr.response == {"confirmed": True, "payload": {"chose_id": "allow_once"}}
    print("OK test_first_of_three_defers_with_pending_name")


def test_second_of_three_also_defers() -> None:
    """One submission already pending; another arrives → still defer."""
    plugin = ConfirmationFormUiPlugin()
    from adk_cc.plugins.confirmation_form_ui import PENDING_CONFIRMATION_NAME
    ctx = _FakeInvocationCtx([
        _wrap_call_event("w1"),
        _wrap_call_event("w2"),
        _wrap_call_event("w3"),
        _user_response_event(
            "w1", PENDING_CONFIRMATION_NAME,
            {"confirmed": True, "payload": {"chose_id": "allow_once"}},
        ),
    ])
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"allow_once": False, "allow_always": False, "deny": True},
        call_id="w2",
    )
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    fr = out.parts[0].function_response
    assert fr.name == PENDING_CONFIRMATION_NAME
    assert fr.id == "w2"
    assert fr.response == {"confirmed": False, "payload": {"chose_id": "deny"}}
    print("OK test_second_of_three_also_defers")


def test_third_of_three_bundles_all() -> None:
    """The final submission collects two stashed pending responses + this
    one and returns a single Content with all three as
    `adk_request_confirmation` — ADK's processor will then resume all
    three tools in one pass."""
    plugin = ConfirmationFormUiPlugin()
    from adk_cc.plugins.confirmation_form_ui import PENDING_CONFIRMATION_NAME
    ctx = _FakeInvocationCtx([
        _wrap_call_event("w1"),
        _wrap_call_event("w2"),
        _wrap_call_event("w3"),
        _user_response_event(
            "w1", PENDING_CONFIRMATION_NAME,
            {"confirmed": True, "payload": {"chose_id": "allow_once"}},
        ),
        _user_response_event(
            "w2", PENDING_CONFIRMATION_NAME,
            {"confirmed": False, "payload": {"chose_id": "deny"}},
        ),
    ])
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"allow_once": False, "allow_always": True, "deny": False},
        call_id="w3",
    )
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    assert out is not None
    # Three function_responses, all with the real confirmation name.
    frs = [p.function_response for p in out.parts if p.function_response]
    assert len(frs) == 3, frs
    by_id = {fr.id: fr for fr in frs}
    assert set(by_id) == {"w1", "w2", "w3"}
    for fr in frs:
        assert fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME, fr
    assert by_id["w1"].response == {"confirmed": True, "payload": {"chose_id": "allow_once"}}
    assert by_id["w2"].response == {"confirmed": False, "payload": {"chose_id": "deny"}}
    assert by_id["w3"].response == {"confirmed": True, "payload": {"chose_id": "allow_always"}}
    print("OK test_third_of_three_bundles_all")


def test_resolved_wraps_no_longer_count() -> None:
    """Wraps resolved in a prior turn (already have an
    `adk_request_confirmation` response) drop out of `unresolved`; a new
    turn with one fresh wrap bundles correctly on the first submit."""
    plugin = ConfirmationFormUiPlugin()
    ctx = _FakeInvocationCtx([
        _wrap_call_event("old-1"),
        _user_response_event(
            "old-1", REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
            {"confirmed": True, "payload": {"chose_id": "allow_once"}},
        ),
        _wrap_call_event("new-1"),
    ])
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"choice": "allow_once"},
        call_id="new-1",
    )
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    fr = out.parts[0].function_response
    # Only `new-1` is unresolved; submission completes the set → bundle.
    assert fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    assert fr.id == "new-1"
    print("OK test_resolved_wraps_no_longer_count")


def test_unknown_wrap_id_falls_into_defer() -> None:
    """Submission for a wrap_id that's not outstanding (stale duplicate,
    spurious submit) gets persisted with the pending sentinel name —
    inert, ADK's processor ignores it, no tool resumes."""
    plugin = ConfirmationFormUiPlugin()
    from adk_cc.plugins.confirmation_form_ui import PENDING_CONFIRMATION_NAME
    ctx = _FakeInvocationCtx([_wrap_call_event("w1")])
    msg = _user_message(
        CONFIRMATION_FORM_FUNCTION_CALL_NAME,
        {"choice": "allow_once"},
        call_id="stale-id",
    )
    out = _run_user_msg(plugin, msg, invocation_context=ctx)
    fr = out.parts[0].function_response
    # Stale: still gets renamed to PENDING (won't satisfy the outstanding
    # `w1`, won't be matched by ADK's processor).
    assert fr.name == PENDING_CONFIRMATION_NAME
    assert fr.id == "stale-id"
    print("OK test_unknown_wrap_id_falls_into_defer")


# --- before_model_callback filter ----------------------------------


class _FakeLlmRequest:
    def __init__(self, contents: list) -> None:
        self.contents = contents


def _run_before_model(
    plugin: ConfirmationFormUiPlugin, req: _FakeLlmRequest
) -> Optional[Any]:
    return asyncio.run(
        plugin.before_model_callback(callback_context=None, llm_request=req)
    )


def test_before_model_filters_pending_confirmation_responses() -> None:
    """Orphan deferred submissions (`adk_cc_pending_confirmation` name)
    should not be sent to the LLM — they're internal bookkeeping that
    look like duplicate/unmatched tool responses to strict providers."""
    from adk_cc.plugins.confirmation_form_ui import PENDING_CONFIRMATION_NAME
    plugin = ConfirmationFormUiPlugin()
    req = _FakeLlmRequest([
        types.Content(role="user", parts=[types.Part(text="please run X")]),
        types.Content(role="user", parts=[
            types.Part(function_response=types.FunctionResponse(
                id="w1", name=PENDING_CONFIRMATION_NAME,
                response={"confirmed": True, "payload": {"chose_id": "allow_once"}},
            ))
        ]),
        types.Content(role="model", parts=[types.Part(text="ok")]),
    ])
    _run_before_model(plugin, req)
    # PENDING content removed; user-text + model-text survive.
    assert len(req.contents) == 2
    assert req.contents[0].parts[0].text == "please run X"
    assert req.contents[1].parts[0].text == "ok"
    print("OK test_before_model_filters_pending_confirmation_responses")


def test_before_model_preserves_non_pending_function_responses() -> None:
    """The filter is targeted at the pending sentinel only.
    `adk_request_confirmation` and regular tool responses pass through."""
    plugin = ConfirmationFormUiPlugin()
    req = _FakeLlmRequest([
        types.Content(role="user", parts=[
            types.Part(function_response=types.FunctionResponse(
                id="w1", name=REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
                response={"confirmed": True, "payload": {"chose_id": "allow_once"}},
            ))
        ]),
        types.Content(role="user", parts=[
            types.Part(function_response=types.FunctionResponse(
                id="t1", name="run_bash", response={"status": "ok"},
            ))
        ]),
    ])
    _run_before_model(plugin, req)
    assert len(req.contents) == 2
    assert req.contents[0].parts[0].function_response.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
    assert req.contents[1].parts[0].function_response.name == "run_bash"
    print("OK test_before_model_preserves_non_pending_function_responses")


def test_before_model_handles_empty_contents() -> None:
    """No contents → no-op, no crash."""
    plugin = ConfirmationFormUiPlugin()
    req = _FakeLlmRequest([])
    _run_before_model(plugin, req)
    assert req.contents == []
    print("OK test_before_model_handles_empty_contents")


def test_before_model_strips_renamed_wrapper_function_call() -> None:
    """The outbound rewrite renames `adk_request_confirmation` →
    `adk_cc_confirmation_form` so the bundled UI takes the form-widget
    path. ADK's contents.py filter `_is_request_confirmation_event`
    only matches the canonical name, so the renamed wrapper would
    otherwise leak into LLM context with its huge schema args and no
    paired tool response (our reshape uses the canonical name which
    IS filtered). Strict providers (sglang) reject the orphan
    tool_call. This filter strips it so the LLM sees the same clean
    history as stock ADK."""
    plugin = ConfirmationFormUiPlugin()
    req = _FakeLlmRequest([
        types.Content(role="user", parts=[types.Part(text="rm /tmp/foo")]),
        types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(
                id="wrap-1",
                name=CONFIRMATION_FORM_FUNCTION_CALL_NAME,
                args={"response_schema": {"type": "object"}, "prompt": "Confirm?"},
            ))
        ]),
        types.Content(role="model", parts=[types.Part(text="done")]),
    ])
    _run_before_model(plugin, req)
    # Sentinel function_call content removed; the surrounding text survives.
    assert len(req.contents) == 2, req.contents
    assert req.contents[0].parts[0].text == "rm /tmp/foo"
    assert req.contents[1].parts[0].text == "done"
    print("OK test_before_model_strips_renamed_wrapper_function_call")


def test_before_model_strips_renamed_wrapper_function_response() -> None:
    """Defensive: function_responses under the sentinel name shouldn't
    occur in session (our `on_user_message_callback` always reshapes to
    the canonical name before persistence), but if one slips through —
    e.g. via a custom frontend submitting the original form-widget
    response without going through the reshape — the filter still
    hides it from the LLM."""
    plugin = ConfirmationFormUiPlugin()
    req = _FakeLlmRequest([
        types.Content(role="user", parts=[
            types.Part(function_response=types.FunctionResponse(
                id="wrap-1",
                name=CONFIRMATION_FORM_FUNCTION_CALL_NAME,
                response={"approve": True},
            ))
        ]),
        types.Content(role="user", parts=[
            types.Part(function_response=types.FunctionResponse(
                id="t1", name="run_bash", response={"status": "ok"},
            ))
        ]),
    ])
    _run_before_model(plugin, req)
    # Only the real run_bash response survives.
    assert len(req.contents) == 1
    assert req.contents[0].parts[0].function_response.name == "run_bash"
    print("OK test_before_model_strips_renamed_wrapper_function_response")


# --- Driver ---------------------------------------------------------


def main() -> None:
    test_build_choice_schema_uses_one_boolean_property_per_option()
    test_build_choice_schema_empty_options_returns_none()
    test_build_choice_schema_skips_reserved_keys()
    test_build_choice_schema_with_comment_adds_textbox()
    test_build_choice_schema_binary_collapses_to_single_boolean()
    test_build_choice_schema_binary_no_deny_id_uses_first_as_positive()
    test_build_choice_schema_binary_deny_first_picks_second_as_positive()
    test_build_choice_schema_without_comment_no_textbox()
    test_extract_comment_helper()
    test_extract_chose_id_accepts_all_shapes()
    test_extract_chose_id_binary_unchecked_returns_negative_id()
    test_extract_chose_id_binary_no_deny_id_uses_second_as_negative()
    test_extract_chose_id_three_options_unchecked_still_returns_none()
    test_outbound_with_comment_flag_adds_textbox_property()
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
    test_inbound_binary_unchecked_form_resolves_to_deny()
    test_inbound_binary_unchecked_with_comment_attaches_comment_to_deny()
    test_inbound_binary_checked_form_resolves_to_approve()
    test_inbound_three_option_deny_wins_over_multi_check()
    test_inbound_comment_folds_into_payload()
    test_inbound_empty_comment_not_included()
    test_inbound_comment_with_approve_passes_through()
    test_inbound_boolean_form_deny_sets_confirmed_false()
    test_inbound_boolean_form_all_false_treated_as_no_choice()
    test_inbound_ignores_non_matching_names()
    test_inbound_handles_empty_parts()
    # Deferred-batch processing
    test_first_of_three_defers_with_pending_name()
    test_second_of_three_also_defers()
    test_third_of_three_bundles_all()
    test_resolved_wraps_no_longer_count()
    test_unknown_wrap_id_falls_into_defer()
    # before_model_callback filter
    test_before_model_filters_pending_confirmation_responses()
    test_before_model_preserves_non_pending_function_responses()
    test_before_model_handles_empty_contents()
    test_before_model_strips_renamed_wrapper_function_call()
    test_before_model_strips_renamed_wrapper_function_response()
    print("\nall confirmation_form_ui tests passed")


if __name__ == "__main__":
    main()
