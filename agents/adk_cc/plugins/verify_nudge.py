"""Soft verification nudge (W9 S2) — free, advisory, in-loop.

Verification today is discretionary: the `verification` sub-agent works (it has
returned real FAILs) but only runs when the coordinator elects to call it. That
is not a loop. This plugin closes the cheap half of the gap.

Mechanism: `before_model_callback` appends a short reminder to the outgoing
request when the turn so far has **asserted a result without executing any
check**, or performed **irreversible/outward-facing actions** unchecked. It
rides the model call that was going to happen anyway — **no extra model call,
no extra latency, no cost beyond a few tokens**. The model may still answer as
it likes; saying "this is unverified" is an acceptable outcome, and a better one
than a confident guess.

Deliberately NOT a hard gate. Sequencing from the plan: ship the free rung,
measure whether it moves the unverified-claim rate, and only then decide whether
the expensive rung (forcing a verification pass) is warranted. A prompt change
alone once took that rate from 3 to 0 in a dogfooding round, so the cheap
mechanism deserves the first shot.

Config: `ADK_CC_VERIFY=off|soft|hard` (default `soft`). `hard` is reserved for
S3 and currently behaves as `soft` — the plugin never blocks.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from ..verification.signals import collect, nudge_text

_log = logging.getLogger(__name__)

_STATE_NUDGED = "temp:verify_nudged_invocation"


def _mode() -> str:
    return (os.environ.get("ADK_CC_VERIFY") or "soft").strip().lower()


class VerifyNudgePlugin(BasePlugin):
    """Injects acceptance criteria at the moment they matter. See module doc."""

    def __init__(self, *, name: str = "adk_cc_verify_nudge") -> None:
        super().__init__(name=name)

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        if _mode() == "off":
            return None
        try:
            ictx = callback_context._invocation_context
            events = ictx.session.events or []
            inv = callback_context.invocation_id
            agent = getattr(ictx.agent, "name", None)
        except AttributeError:  # internals shifted — never break a turn
            return None

        # This turn only: earlier turns' claims are already answered for.
        turn = [e for e in events if getattr(e, "invocation_id", None) == inv]
        if not turn:
            return None

        sig = collect(turn, author=agent)
        # Predictive: at before_model the final answer (and its claim) does not
        # exist yet, so key on 'changed something, checked nothing'.
        text = nudge_text(sig, predictive=True)
        if not text:
            return None

        # Once per invocation — a reminder repeated every step becomes wallpaper.
        state = getattr(callback_context, "state", None)
        if state is not None:
            if state.get(_STATE_NUDGED) == inv:
                return None
            try:
                state[_STATE_NUDGED] = inv
            except Exception:  # noqa: BLE001 — state is best-effort here
                pass

        _log.info("verify nudge: %s", sig.summary())
        _append_to_system_instruction(llm_request, text)
        return None


def _append_to_system_instruction(req: LlmRequest, text: str) -> None:
    """Same shape as the other adk-cc prompt-injecting plugins."""
    cfg = req.config
    existing = cfg.system_instruction
    if existing is None:
        cfg.system_instruction = text
    elif isinstance(existing, str):
        if text not in existing:
            cfg.system_instruction = existing + "\n\n" + text
    else:
        try:
            parts = list(existing) if isinstance(existing, list) else [existing]
            parts.append(types.Part(text=text))
            cfg.system_instruction = parts
        except Exception:  # noqa: BLE001 — guidance is best-effort
            pass
