"""Bridge the structured `ConfirmPrompt` payload to bundled `adk web`'s
long-running form widget.

PR #1 wired `PermissionPlugin`'s ask branch to send a structured
`ConfirmPrompt` payload via ADK's `request_confirmation`. ADK emits an
`adk_request_confirmation` function-call event. The bundled `adk web`
UI short-circuits this event into its **binary** widget (checkbox +
read-only payload textarea + Submit) because of a literal name check
(`main-*.js`):

    get isConfirmationRequest() {
        return this.functionCall?.name === "adk_request_confirmation"
    }
    if (isConfirmationRequest)        → binary widget   ← always taken
    else if (response_schema + ...)   → form widget
    else                              → free-form textarea

So a `ConfirmPrompt` with N options renders as a single checkbox. This
plugin rewrites both directions of the protocol so the bundled UI takes
the form-widget path instead.

## Why one-boolean-per-option (not a `string`/`enum`)

Bundled `adk web`'s form-widget renders fields by JSON Schema `type`
only (see `initForm()` in `main-*.js`):

    if (n === "boolean")  → checkbox input
    else if (n === "integer" || n === "number") → numeric input
    else (incl. "string") → free-form text input

`enum` is **not consulted** — a `{type: "string", enum: [...]}` schema
renders as a plain textbox where the operator has to type one of the
ids manually (and a typo silently denies the operation). The only path
to a real "pick one" UI in the bundled form is **one boolean field per
option**: each option renders as a checkbox, the operator ticks one,
and on submit the response is `{<chose_id_a>: false, <chose_id_b>:
true, ...}`. The plugin then maps the first True-valued key to
`chose_id` and reshapes for ADK's resume processor.

Yes, it's awkward that "pick one of N" is rendered as N checkboxes
rather than a radio group or dropdown — but the bundled UI has no
form-side support for either. The label + description on each checkbox
makes the intent clear, and the inbound side accepts a clean
single-true vote.

## OUTBOUND (`on_event_callback`)

  - Find function-call events whose name is `adk_request_confirmation`.
  - Build a `response_schema` where each `ConfirmPrompt.options[i]`
    becomes a boolean property keyed on the option's id. The
    description on each property is `<label> — <option description>`.
  - Inject the schema into the call's args.
  - Also write `args.prompt` to the prompt's title (+ detail when set)
    so the bundled UI shows it above the form.
  - Rename the call's name from `adk_request_confirmation` to
    `adk_cc_confirmation_form` (no leading underscore — some
    OpenAI-compatible backends reject names starting with `_`) so the
    bundled UI's confirmation short-circuit doesn't trigger and the
    form-widget path takes over.
  - The function-call **id is preserved**. ADK's resume processor
    matches on id, not on name, so the rename is transparent to resume.

## INBOUND (`on_user_message_callback`)

  - When the user submits a function_response under the sentinel name,
    accept any of:
      - `{<chose_id>: true, ...}` — bundled UI form output. Take the
        first True-valued key as the chose_id.
      - `{choice: <id>}` — legacy bundled-UI shape (when we shipped a
        `string`/`enum` schema in v1 of this plugin).
      - `{chose_id: <id>}` — payload-aware custom frontend.
      - `{result: <id>}` — bundled UI's free-form textarea fallback.
  - Reshape the response to ADK's standard
    `{confirmed: <bool>, payload: {chose_id: <id>}}` (confirmed =
    chose_id != "deny").
  - Rename the function_response back to `adk_request_confirmation` so
    ADK's `_RequestConfirmationLlmRequestProcessor` finds it and resumes
    the gated tool exactly as before.

The `toolConfirmation.payload` (rich `ConfirmPrompt`) stays in the
renamed event's args, so payload-aware custom frontends can read the
full structure if they listen for both names.

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
# on outbound events. The bundled UI's confirmation short-circuit
# (`name === "adk_request_confirmation"`) does not match this, so the
# UI proceeds to its form-widget path.
#
# Naming: stick to `[a-zA-Z][a-zA-Z0-9_]*`. OpenAI's spec permits
# leading underscores in function names (`^[a-zA-Z0-9_-]+$`) but
# stricter backends (e.g. sglang's OpenAI-compatible endpoint) reject
# names starting with `_` — those become role-like control tokens in
# some chat templates. The `adk_cc_` prefix is enough to namespace us
# away from real ADK names without the underscore tripping validators.
CONFIRMATION_FORM_FUNCTION_CALL_NAME = "adk_cc_confirmation_form"

# Keys we should NEVER treat as chose_ids when scanning the response
# for a True-valued field. Some are part of the ADK ToolConfirmation
# shape (`confirmed`), others are legacy shapes (`choice`, `chose_id`)
# that we handle in their own branches.
_RESERVED_RESPONSE_KEYS = frozenset({"confirmed", "choice", "chose_id", "result"})


class ConfirmationFormUiPlugin(BasePlugin):
    """Make `adk_request_confirmation` events render as the bundled UI's
    long-running form widget (one checkbox per option) instead of its
    binary confirmation widget.

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
            schema = _build_choice_schema(options)
            if schema is None:
                continue
            args["response_schema"] = schema
            # The bundled UI reads `prompt` (or `message`) and shows it
            # above the form. Without this, the prompt section just
            # displays "Please provide your response".
            prompt_text = _build_prompt_text(
                payload.get("title"), payload.get("detail")
            )
            if prompt_text:
                args["prompt"] = prompt_text
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


def _build_choice_schema(options: list) -> Optional[dict]:
    """Convert `ConfirmPrompt.options` to a JSON Schema renderable by the
    bundled UI's form widget as one checkbox per option.

    Each option becomes a `boolean` property keyed on its `id`. The
    `description` is `<label> — <option description>` so the operator
    sees the human-readable label next to the checkbox.
    """
    properties: dict[str, Any] = {}
    for opt in options:
        if not isinstance(opt, dict):
            continue
        oid = opt.get("id")
        if not isinstance(oid, str) or not oid:
            continue
        if oid in _RESERVED_RESPONSE_KEYS:
            # Refuse to shadow a reserved response key — would confuse
            # the inbound disambiguation. Skip; the prompt still works
            # via the other options.
            continue
        label = opt.get("label") or oid
        desc = opt.get("description") or ""
        properties[oid] = {
            "type": "boolean",
            "description": f"{label} — {desc}" if desc else label,
        }
    if not properties:
        return None
    return {"type": "object", "properties": properties}


def _build_prompt_text(title: Optional[str], detail: Optional[str]) -> str:
    """Format the prompt text shown above the form. The bundled UI reads
    `args.prompt` and renders it as the prompt heading."""
    parts: list[str] = []
    if title:
        parts.append(str(title).strip())
    if detail and str(detail).strip() != (parts[0] if parts else ""):
        parts.append(str(detail).strip())
    return "\n\n".join(p for p in parts if p)


def _extract_chose_id(response: Any) -> Optional[str]:
    """Pull a chose_id from any of the supported response shapes.

    Order:
      1. `choice` / `chose_id` (legacy / payload-aware)
      2. `result` (bundled UI's free-form textarea fallback)
      3. First True-valued non-reserved key (current bundled UI form)
    """
    if not isinstance(response, dict):
        return None
    # Single-value shapes for custom frontends + legacy payload protocol.
    for key in ("choice", "chose_id"):
        val = response.get(key)
        if isinstance(val, str) and val:
            return val
    # Bundled UI free-form fallback.
    result = response.get("result")
    if isinstance(result, str) and result:
        return result
    # Boolean-per-option shape (current bundled UI form output). Use the
    # first True-valued key in iteration order — Python dicts preserve
    # insertion order, so this matches the option order from the schema.
    for key, val in response.items():
        if key in _RESERVED_RESPONSE_KEYS:
            continue
        if val is True:
            return key
    return None
