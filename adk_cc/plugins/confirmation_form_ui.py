"""Bridge the structured `ConfirmPrompt` payload to bundled `adk web`'s
long-running form widget.

PR #1 wired `PermissionPlugin`'s ask branch to send a structured
`ConfirmPrompt` payload via ADK's `request_confirmation`. ADK in turn
emits an `adk_request_confirmation` function-call event. The bundled
`adk web` UI short-circuits this event into its **binary** widget
(checkbox + read-only payload textarea + Submit) because of a literal
name check (`main-*.js`):

    get isConfirmationRequest() {
        return this.functionCall?.name === "adk_request_confirmation"
    }
    if (isConfirmationRequest)        → binary widget   ← always taken
    else if (response_schema + ...)   → form widget
    else                              → free-form textarea

So a `ConfirmPrompt` with N options renders as a single checkbox. This
plugin rewrites both directions of the protocol so the bundled UI takes
the form-widget path instead, displaying a dropdown of N option ids.

OUTBOUND (`on_event_callback`):

  - Find function-call events whose name is `adk_request_confirmation`.
  - Build a `response_schema` from the `ConfirmPrompt`'s `options`: one
    `string` property `choice` whose `enum` is the option ids.
  - Inject the schema into the call's args, then **rename** the call's
    name from `adk_request_confirmation` to a sentinel
    (`_adk_cc_confirmation_form`) so the bundled UI's confirmation
    short-circuit doesn't trigger. The bundled UI then takes the form
    branch and renders a dropdown.
  - The function-call id is preserved — ADK's resume processor matches
    on id, not on name, so renaming doesn't break the resume.

INBOUND (`on_user_message_callback`):

  - When the user submits a function_response whose name matches our
    sentinel, accept either `{choice: <id>}` (bundled UI form) or
    `{chose_id: <id>}` (custom frontend) as the shape.
  - Rewrite the response to ADK's standard
    `{confirmed: <bool>, payload: {chose_id: <id>}}` shape.
  - Rename the function_response back to `adk_request_confirmation` so
    ADK's `_RequestConfirmationLlmRequestProcessor` finds it and resumes
    the gated tool exactly as it would without this plugin.

The `toolConfirmation.payload` (rich `ConfirmPrompt`) is preserved in
the renamed event's args, so payload-aware custom frontends can read
the full structure if they listen for both names.

Disabling this plugin reverts to the binary widget without breaking
anything — `PermissionPlugin` and ADK's request_confirmation flow run
unchanged underneath.
"""
from __future__ import annotations

from typing import Any, Optional

from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.flows.llm_flows.functions import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

# Sentinel function name that swaps in for `adk_request_confirmation`
# on outbound events. The leading underscore signals "internal"; the
# `_adk_cc_` prefix avoids any collision with real ADK names. The
# bundled UI's confirmation short-circuit (`name === "adk_request_confirmation"`)
# does not match, so the UI proceeds to its form-widget path.
CONFIRMATION_FORM_FUNCTION_CALL_NAME = "_adk_cc_confirmation_form"


class ConfirmationFormUiPlugin(BasePlugin):
    """Make `adk_request_confirmation` events render as the bundled UI's
    long-running form widget instead of its binary confirmation widget.

    See module docstring for the full rewrite contract on both sides.
    """

    def __init__(self, name: str = "confirmation_form_ui") -> None:
        super().__init__(name=name)

    async def on_event_callback(
        self,
        *,
        invocation_context: InvocationContext,
        event: Event,
    ) -> Optional[Event]:
        if not event.content or not event.content.parts:
            return None
        mutated = False
        for part in event.content.parts:
            fc = getattr(part, "function_call", None)
            if fc is None or fc.name != REQUEST_CONFIRMATION_FUNCTION_CALL_NAME:
                continue
            args = dict(fc.args or {})
            tool_conf = args.get("toolConfirmation")
            if not isinstance(tool_conf, dict):
                continue
            payload = tool_conf.get("payload")
            if not isinstance(payload, dict):
                continue
            options = payload.get("options")
            if not isinstance(options, list) or not options:
                continue
            schema = _build_choice_schema(options, payload.get("title"))
            if schema is None:
                continue
            args["response_schema"] = schema
            fc.args = args
            fc.name = CONFIRMATION_FORM_FUNCTION_CALL_NAME
            mutated = True
        return event if mutated else None

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> Optional[types.Content]:
        if not user_message.parts:
            return None
        mutated = False
        for part in user_message.parts:
            fr = getattr(part, "function_response", None)
            if fr is None or fr.name != CONFIRMATION_FORM_FUNCTION_CALL_NAME:
                continue
            chose_id = _extract_chose_id(fr.response)
            if chose_id is None:
                # Unrecognized response shape; leave it alone so any error is
                # visible rather than silently swallowed.
                continue
            fr.name = REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
            fr.response = {
                "confirmed": chose_id != "deny",
                "payload": {"chose_id": chose_id},
            }
            mutated = True
        return user_message if mutated else None


def _build_choice_schema(options: list, title: Optional[str]) -> Optional[dict]:
    """Convert `ConfirmPrompt.options` to a JSON Schema renderable by the
    bundled UI's form widget.

    The schema has a single `choice` property with `enum` set to the
    option `id`s. The `description` lists the human-readable label and
    description for each id so the operator sees what they're picking.
    """
    enum_values: list[str] = []
    description_lines: list[str] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        oid = opt.get("id")
        if not isinstance(oid, str):
            continue
        enum_values.append(oid)
        label = opt.get("label") or oid
        desc = opt.get("description") or ""
        line = f"- {oid}: {label}"
        if desc:
            line += f" — {desc}"
        description_lines.append(line)
    if not enum_values:
        return None
    description = (title or "Choose an option").strip()
    if description_lines:
        description = description + "\n\n" + "\n".join(description_lines)
    return {
        "type": "object",
        "properties": {
            "choice": {
                "type": "string",
                "enum": enum_values,
                "description": description,
            }
        },
        "required": ["choice"],
    }


def _extract_chose_id(response: Any) -> Optional[str]:
    """Pull a chose_id from either the bundled-UI form shape (`choice`)
    or the legacy payload-aware shape (`chose_id`)."""
    if not isinstance(response, dict):
        return None
    for key in ("choice", "chose_id"):
        val = response.get(key)
        if isinstance(val, str) and val:
            return val
    # Bundled UI may also send {result: <text>} if the form widget falls
    # back to free-form. Try to recover a chose_id from that text.
    result = response.get("result")
    if isinstance(result, str) and result:
        return result
    return None
