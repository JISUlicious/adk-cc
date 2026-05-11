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

## Deferred-batch processing (multi-tool confirmation)

When the model emits N gated tool calls in one turn, ADK emits N
wrapper events (`adk_cc_confirmation_form`). Bundled `adk web` renders
N independent widgets; each Submit click fires a separate HTTP
request. By default ADK's `_RequestConfirmationLlmRequestProcessor`
would resume each tool the moment its widget is submitted — N
separate resume cycles with an LLM call between each. The operator
sees the agent half-act between every click, which is jarring and
expensive in LLM tokens.

`on_user_message_callback` defers each submission until ALL
outstanding wraps have been answered, then bundles them into one user
event. ADK's processor scans that single event and resumes all N
tools in one pass via `handle_function_call_list_async`. One LLM call
follows with all N results in context.

Mechanism: deferred submissions are persisted with a non-matching
sentinel name (`adk_cc_pending_confirmation`) so ADK's processor
ignores them. Outstanding wrap_ids are computed from session events
(function_calls with the sentinel form name minus those already
resolved by an `adk_request_confirmation` response). When the latest
submission completes the set, all stashed responses are pulled from
prior session events, reshaped to the standard `ToolConfirmation`
shape, and emitted as one bundled user event with proper names.

The deferred responses persist with the session (durable across
restarts when `ADK_CC_SESSION_DSN` is configured), so the operator
can pause a batch and come back later. The orphan
`adk_cc_pending_confirmation` responses sit in session history as
inert bookkeeping — `before_model_callback` filters them from the
LLM's view so the model sees a clean history.

Disabling this plugin reverts to the binary widget without breaking
anything — `PermissionPlugin` and ADK's request_confirmation flow run
unchanged underneath.
"""
from __future__ import annotations

from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.flows.llm_flows.functions import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
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

# Sentinel function name for deferred submissions. ADK's
# `_RequestConfirmationLlmRequestProcessor` only matches the literal
# `adk_request_confirmation` name, so anything else (including this
# sentinel) is ignored — which is exactly what we want for "hold this
# until all are in". When the last submission arrives, the deferred
# responses are pulled back out of session history, renamed to the
# real name, and bundled into one user event so the processor resumes
# all tools in one pass.
PENDING_CONFIRMATION_NAME = "adk_cc_pending_confirmation"


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

        # Collect incoming function_response parts that target our sentinel.
        incoming: list[types.FunctionResponse] = []
        other_parts: list[types.Part] = []
        for part in user_message.parts:
            fr = getattr(part, "function_response", None)
            if fr is not None and fr.name == CONFIRMATION_FORM_FUNCTION_CALL_NAME:
                incoming.append(fr)
            else:
                other_parts.append(part)

        if not incoming:
            return None  # nothing addressed to this plugin

        # Reshape each incoming response to the standard ToolConfirmation
        # shape ({confirmed: bool, payload: {chose_id: <id>}}).
        reshaped_incoming: dict[str, dict] = {}
        for fr in incoming:
            chose_id = _extract_chose_id(fr.response)
            if chose_id is None:
                # Unrecognized response shape — let it through unmodified so
                # any error surfaces rather than getting silently swallowed.
                continue
            reshaped_incoming[fr.id or ""] = {
                "confirmed": chose_id != "deny",
                "payload": {"chose_id": chose_id},
            }

        if not reshaped_incoming:
            return None  # all incoming responses were unparseable

        events = _session_events(invocation_context)
        outstanding = _outstanding_wrap_ids(events)
        already_pending = _stashed_pending_responses(events)

        # Anything not in `outstanding - resolved` is either stale (already
        # resumed) or unknown (defensive). Filter both incoming and pending
        # down to the relevant set.
        unresolved = outstanding - _resolved_wrap_ids(events)
        relevant_incoming = {
            k: v for k, v in reshaped_incoming.items() if k in unresolved
        }
        relevant_pending = {
            k: v for k, v in already_pending.items() if k in unresolved
        }
        union_ids = set(relevant_incoming) | set(relevant_pending)

        if not unresolved or not unresolved.issubset(union_ids):
            # Either no outstanding wraps to bundle yet (defensive), or
            # not all wraps have responses yet. Persist every reshaped
            # submission under the deferred sentinel name so ADK's
            # processor ignores it; the bundle step filters out stale
            # ids later, so renaming uniformly here is safe.
            new_parts = list(other_parts)
            for fr in incoming:
                if fr.id in reshaped_incoming:
                    new_parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=fr.id,
                                name=PENDING_CONFIRMATION_NAME,
                                response=reshaped_incoming[fr.id],
                            )
                        )
                    )
                else:
                    # Unparseable response — leave the original part so
                    # the operator sees the natural error rather than a
                    # silently-deferred mystery vote.
                    new_parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=fr.id, name=fr.name, response=fr.response
                            )
                        )
                    )
            if not new_parts:
                return None
            return types.Content(role=user_message.role, parts=new_parts)

        # All in. Bundle every relevant response into one user event with
        # the real `adk_request_confirmation` name. ADK's resume processor
        # scans this single event and runs all tools in one pass.
        bundle: dict[str, dict] = dict(relevant_pending)
        bundle.update(relevant_incoming)  # incoming wins if both have an id

        bundle_parts: list[types.Part] = []
        for wrap_id, response in bundle.items():
            bundle_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=wrap_id,
                        name=REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
                        response=response,
                    )
                )
            )
        # Preserve any non-confirmation parts the operator's UI may have
        # included alongside the function_response (rare; defensive).
        bundle_parts.extend(other_parts)
        return types.Content(role=user_message.role, parts=bundle_parts)

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        """Two jobs:

        1. **Hide deferred bookkeeping** — filter
           `adk_cc_pending_confirmation` function_responses out of
           `llm_request.contents` so the LLM never sees them. These are
           internal stash entries that look like duplicate / orphan tool
           responses to strict providers (sglang's OpenAI-compatible
           endpoint, OpenAI tool_calls validation, etc).

        2. **Short-circuit the LLM call when a batch is still in flight**
           — if we're in deferred-batch mode (outstanding wraps that
           haven't all been answered yet), return an empty `LlmResponse`
           so ADK skips the actual model call. The operator's HTTP
           request returns quickly with no agent action; the next
           submission re-enters the flow. This avoids wasted LLM
           round-trips per click.

        Pure read-side hygiene on (1); short-circuit on (2). Session
        events are unchanged on disk either way.
        """
        contents = getattr(llm_request, "contents", None) or []

        # Job 1: filter pending sentinels.
        mutated = False
        new_contents: list[types.Content] = []
        for content in contents:
            kept_parts: list[types.Part] = []
            for part in (content.parts or []):
                fr = getattr(part, "function_response", None)
                if fr is not None and fr.name == PENDING_CONFIRMATION_NAME:
                    mutated = True
                    continue
                kept_parts.append(part)
            if kept_parts:
                new_contents.append(
                    types.Content(role=content.role, parts=kept_parts)
                )
            elif content.parts:
                # Content had only pending-sentinel parts — drop it.
                mutated = True
        if mutated:
            llm_request.contents = new_contents

        # Job 2: short-circuit if a deferred batch is still in flight.
        # Compute outstanding vs resolved from session events; if there
        # are any outstanding wraps with no `adk_request_confirmation`
        # response yet, we're mid-batch and shouldn't waste an LLM call.
        events: list = []
        try:
            session_events = getattr(callback_context.session, "events", None)
            if session_events:
                events = list(session_events)
        except Exception:
            pass
        if events:
            outstanding = _outstanding_wrap_ids(events)
            resolved = _resolved_wrap_ids(events)
            if outstanding - resolved:
                # At least one wrap is unresolved. Skip the LLM call.
                return LlmResponse()

        return None


def _session_events(invocation_context) -> list:
    """Best-effort fetch of session events for the deferred-batch logic.
    Returns an empty list when the context, session, or events are
    missing — keeps the plugin testable with a fake context and tolerant
    of unusual session shapes."""
    if invocation_context is None:
        return []
    session = getattr(invocation_context, "session", None)
    if session is None:
        return []
    events = getattr(session, "events", None)
    if not events:
        return []
    return list(events)


def _outstanding_wrap_ids(events) -> set[str]:
    """Return the set of wrap_call_ids for every confirmation wrapper
    event ADK ever emitted in this session (renamed to our sentinel by
    `on_event_callback`). Includes both currently-pending wraps and
    already-resolved ones; callers subtract `_resolved_wrap_ids` to get
    the live set."""
    ids: set[str] = set()
    for ev in events:
        for fc in ev.get_function_calls():
            if fc.name == CONFIRMATION_FORM_FUNCTION_CALL_NAME and fc.id:
                ids.add(fc.id)
    return ids


def _resolved_wrap_ids(events) -> set[str]:
    """Return wrap_call_ids that have already gone through ADK's resume
    processor — i.e. are referenced by a `function_response` whose name
    is the canonical `adk_request_confirmation`. These came from a
    successful bundle and the wrapped tool has already been re-run."""
    ids: set[str] = set()
    for ev in events:
        if getattr(ev, "author", None) != "user":
            continue
        for fr in ev.get_function_responses():
            if fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME and fr.id:
                ids.add(fr.id)
    return ids


def _stashed_pending_responses(events) -> dict[str, dict]:
    """Return wrap_call_id → ToolConfirmation-shaped response for every
    submission persisted earlier under the deferred sentinel name.

    The bundle at the final-submission step pulls from this so the
    operator's earlier votes don't get lost.
    """
    pending: dict[str, dict] = {}
    for ev in events:
        if getattr(ev, "author", None) != "user":
            continue
        for fr in ev.get_function_responses():
            if fr.name != PENDING_CONFIRMATION_NAME:
                continue
            if not fr.id:
                continue
            if isinstance(fr.response, dict):
                pending[fr.id] = fr.response
    return pending


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
