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

`hard` adds the S3 GATE, justified by measurement rather than taste: a paired
experiment (scripts/measure_verify_nudge.py) found the advisory nudge moved only
1 of 5 mutate-then-report turns (off 0/5 verified, soft 1/5). The model reads
the reminder and mostly proceeds anyway.

The gate lives in `after_model_callback`, which matters: that is the FIRST
moment the claim exists. `before_model` cannot see it — the claim is in the
response being generated — which is precisely why the soft rung is weak. At
`after_model` the plugin can compare the claim against the turn's evidence and,
finding none, replace the answer with a transfer to the `verification`
sub-agent. ADK honours the replacement
(`if (altered := await _handle_after_model_callback(...)): llm_response =
altered`), so the forged call is processed as a normal transfer.

Config: `ADK_CC_VERIFY=off|soft|hard` (default `soft`).

STATUS — `hard` is opt-in (default `soft`) but now complete end-to-end: the
gate fires on an unverified claim, routes to `verification`, which runs real
checks and returns a verdict, and the coordinator then answers the user
normally. Live runs have returned FAIL and PARTIAL on changes the coordinator
had already called done — exactly the value.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from ..verification.signals import (
    _CLAIM_RE,
    _HEDGE_RE,
    _LEAD_CLAIM_RE,
    collect,
    noop_subagent_reentry,
    nudge_text,
    reentry_note,
    undriven_pages,
)

_log = logging.getLogger(__name__)

# The vocabulary of a claim ABOUT page behaviour — what a user sees or does.
# Guards the cross-turn label: a session with an undriven page must not label
# an unrelated "Done — renamed the file".
# An AFFIRMATION — the claim shape of answering "does it work?" with "yes".
# The shared _CLAIM_RE is completion-report shaped ("done", "fixed", "works
# now") and missed both live turn-3 answers, which led with "Yes — the colour
# control does change …". Scoped to the cross-turn path only, so the same-turn
# signals keep their measured recall/noise balance.
_AFFIRM_RE = re.compile(
    r"(?i)^\s*(?:\*\*)?(?:yes|correct|indeed|it works|absolutely)\b")

_BEHAVIOUR_RE = re.compile(
    r"\b(page|preview|click|button|control|swatch|visitor|browser|render|"
    r"display|updates?|changes)\b")

_STATE_NUDGED = "temp:verify_nudged_invocation"
_STATE_LABELED = "temp:verify_labeled_invocation"
_STATE_GATED = "temp:verify_gated_invocation"
_STATE_REENTRY = "temp:verify_reentry_invocation"
_VERIFIER = "verification"


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

        # A silently no-op'd re-verification outranks the ordinary nudge: the
        # coordinator is about to report a verdict that cannot exist.
        if noop_subagent_reentry(turn, agent=_VERIFIER):
            state0 = getattr(callback_context, "state", None)
            if state0 is None or state0.get(_STATE_REENTRY) != inv:
                if state0 is not None:
                    try:
                        state0[_STATE_REENTRY] = inv
                    except Exception:  # noqa: BLE001
                        pass
                _log.info("verification re-entry was a no-op; telling the coordinator")
                _append_to_system_instruction(llm_request, reentry_note(_VERIFIER))
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


    # ---- S3: the hard gate -------------------------------------------------

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> Optional[LlmResponse]:
        """Block an unverified claim by routing to the verifier instead.

        Fires only when ALL hold: mode is `hard`; this is a final text answer
        (not a tool call); the turn changed something; nothing checked it; and
        the answer asserts a result (or the turn did something irreversible).
        Bounded to once per invocation, so a FAIL cannot ping-pong.

        SOFT mode also acts here, for one narrow case the request-side nudge
        can never reach: a behaviour claim about a page built in an EARLIER
        turn. `before_model` sees only the user's question then, and the claim
        does not exist until this response — so the honest thing left is to
        LABEL it: append one visible line saying the page has not been driven
        since it changed. Measured (three live runs): without this, a true
        claim and a shipped falsehood read identically to the user.
        """
        if _mode() == "off":
            return None
        content = getattr(llm_response, "content", None)
        parts = list(getattr(content, "parts", None) or [])
        if not parts:
            return None
        # A tool call is not an answer — let the turn continue.
        if any(getattr(p, "function_call", None) is not None for p in parts):
            return None
        if getattr(llm_response, "partial", False):
            return None
        answer = "\n".join(
            p.text for p in parts
            if getattr(p, "text", None) and not getattr(p, "thought", False)
        ).strip()
        if not answer:
            return None

        try:
            ictx = callback_context._invocation_context
            inv = callback_context.invocation_id
            agent = getattr(ictx.agent, "name", None)
            events = ictx.session.events or []
        except AttributeError:
            return None

        # Never gate the verifier itself — it would recurse.
        if agent == _VERIFIER:
            return None

        state = getattr(callback_context, "state", None)
        if state is not None and state.get(_STATE_GATED) == inv:
            return None

        turn = [e for e in events if getattr(e, "invocation_id", None) == inv]
        if not turn:
            return None
        # Already verified this turn? Then nothing to gate.
        if any(getattr(e, "author", None) == _VERIFIER for e in turn):
            return None

        sig = collect(turn, author=agent)
        if sig.has_evidence:
            return None
        claims = bool(_CLAIM_RE.search(answer)) or bool(_LEAD_CLAIM_RE.match(answer))

        # The cross-turn case: a claim about a page nothing has driven since it
        # last changed. Requires the answer to be ABOUT the page — its name, or
        # the vocabulary of page behaviour — so an unrelated "Done — renamed
        # the file" in the same session is not labelled.
        stale = ()
        if (claims or _AFFIRM_RE.match(answer)) and not _HEDGE_RE.search(answer):
            pages = undriven_pages(events)
            low = answer.lower()
            stale = tuple(
                pg for pg in pages
                if os.path.basename(pg).lower() in low or _BEHAVIOUR_RE.search(low))

        if _mode() == "soft":
            if not stale:
                return None
            if state is not None:
                if state.get(_STATE_LABELED) == inv:
                    return None
                try:
                    state[_STATE_LABELED] = inv
                except Exception:  # noqa: BLE001
                    pass
            _log.info("verify label: stale page claim about %s", stale)
            note = (
                f"\n\n> ⚠ adk-cc: `{os.path.basename(stale[0])}` has changed "
                "since it was last actually driven — the behaviour described "
                "above is asserted, not verified. The web-smoke-check skill "
                "can close that gap."
            )
            labeled = llm_response.model_copy(deep=True)
            try:
                labeled.content.parts.append(types.Part(text=note))
            except Exception:  # noqa: BLE001 — labelling must never eat the answer
                return None
            return labeled

        # hard: the gate. A stale cross-turn claim now qualifies alongside the
        # original same-turn trigger.
        if not sig.changed_anything and not stale:
            return None
        if not (claims or sig.risky):
            return None

        if state is not None:
            try:
                state[_STATE_GATED] = inv
            except Exception:  # noqa: BLE001
                pass
        _log.info("verify GATE: routing to %s (%s)", _VERIFIER, sig.summary())

        # ONLY the transfer — no text part. A text part here is recorded as a
        # coordinator message and becomes the user-visible answer (observed
        # live: the gate's own scaffolding was shown as the reply). The reason
        # belongs in the log and in the transfer, not in the conversation.
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(function_call=types.FunctionCall(
                        # A REAL id is mandatory. An id-less call pairs with an
                        # id-less response, and ADK's content rearrangement
                        # then cannot match them — the coordinator's next
                        # request is malformed and it returns EMPTY content, so
                        # the turn ends with no answer (observed: three empty
                        # coordinator events after the handback). Same failure
                        # mode as the id-less _handback_to_coordinator marker.
                        id=f"verifygate-{uuid.uuid4().hex[:8]}",
                        name="transfer_to_agent",
                        args={"agent_name": _VERIFIER},
                    )),
                ],
            )
        )


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
