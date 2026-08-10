"""Pin parked confirmations against ADK event compaction (#119).

Reported live: tool call gated -> waiting on the user -> compaction ran ->
answering the card raised ADK's "No function call event found for function
responses ids: {...}" (flows/llm_flows/contents.py).

ADK's compactor already refuses to fold events carrying a PENDING function
call (`apps/compaction.py _pending_function_call_ids`), but its notion of
"answered" is any function_response with a matching id. adk-cc's
confirmation protocol produces two responses that are NOT answers:

  - the gates close the ORIGINAL call with an interim dict so the turn can
    end parked on the user — ``{"status": "needs_confirmation", ...}`` from
    the permission plugin (permissions.py) and
    ``{"status": "awaiting_user_confirmation"}`` from approval-gated tools
    like exit_plan_mode (tools/base.py) — and
  - a stashed first click in a batch answers the WRAP id under the sentinel
    name ``adk_cc_pending_confirmation`` that ADK deliberately ignores
    (confirmation_form_ui.py).

(ask_user_question needs nothing here: it returns None, so its call stays
response-less and ADK's stock guard already pins it.)

Either one makes the parked call look answered, ADK compacts the call
event, and the post-approval real response orphans. The fix mirrors the
broker's name-aware rule (turns.py `_answered_ids`): pending = every call
id minus REAL answer ids. Installed by replacing the module function —
both selection paths (token-threshold and sliding-window) resolve it as a
module global, so one patch covers both, and ADK's own prefix truncation
(`_truncate_events_before_pending_function_call`) then keeps everything
from the first parked call onward — which also preserves stashed clicks.

Wrapping a private ADK function follows the `install_confirmation_resume_fix`
precedent: tests/test_compaction_pin.py pins the private-API contract AND
A/Bs the stock vulnerability, so an ADK upgrade that renames the hook or
fixes the hole fails loudly there instead of silently diverging here.

Deliberate tradeoff: a confirmation nobody ever answers pins its suffix of
the session against event compaction indefinitely (prefix compaction still
runs; microcompact and the context guard still bound the request size).
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

# Same literal pair as turns.py — kept a literal so this module imports no
# plugin code; the contract test pins all three sites to the plugin's names.
_UNDELIVERED_CONFIRMATION_NAMES = ("adk_cc_pending_confirmation",
                                   "adk_cc_confirmation_form")

# The gates' interim statuses (permissions.py / tools/base.py). Contract
# test pins these against the producing source.
_PARKED_STATUSES = ("needs_confirmation", "awaiting_user_confirmation")


def _is_real_answer(fr: Any) -> bool:
    """False for responses that leave the call parked on the user."""
    if getattr(fr, "name", "") in _UNDELIVERED_CONFIRMATION_NAMES:
        return False  # stashed / pre-rewrite: ADK never delivered it
    resp = getattr(fr, "response", None)
    if isinstance(resp, dict) and resp.get("status") in _PARKED_STATUSES:
        return False  # a gate's interim close; the real result comes later
    return True


def _pending_ids_name_aware(events: list) -> set[str]:
    """Drop-in for ADK's `_pending_function_call_ids`, minus false answers."""
    calls: set[str] = set()
    real: set[str] = set()
    for event in events:
        for fc in event.get_function_calls():
            if fc.id:
                calls.add(fc.id)
        for fr in event.get_function_responses():
            if fr.id and _is_real_answer(fr):
                real.add(fr.id)
    return calls - real


def install_compaction_pin() -> bool:
    """Patch ADK's pending-call computation. Idempotent. True on success."""
    try:
        from google.adk.apps import compaction
    except ImportError:
        _log.warning("compaction pin: google.adk.apps.compaction unavailable "
                     "— skipped")
        return False
    if getattr(compaction, "_adk_cc_pin", False):
        return True
    if not callable(getattr(compaction, "_pending_function_call_ids", None)):
        _log.warning(
            "compaction pin: ADK no longer exposes _pending_function_call_ids "
            "— parked confirmations are NOT protected from compaction; "
            "update compaction_pin.py for this ADK version")
        return False
    compaction._pending_function_call_ids = _pending_ids_name_aware
    compaction._adk_cc_pin = True
    _log.info("compaction pin installed: parked confirmations excluded from "
              "event compaction")
    return True
