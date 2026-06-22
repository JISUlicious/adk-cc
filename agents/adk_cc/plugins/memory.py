"""Autonomous memory plugin (ADK_CC_MEMORY=1): recall + capture.

Memory is the AUTONOMOUS subsystem (vs. the explicit wiki). This plugin gives
it both halves without the agent ever calling a tool:

  1. RECALL (read, cheap — no model call): before_model_callback injects a
     budgeted block of the user's relevant memories (semantic first) into the
     system instruction every turn.

  2. CAPTURE (write, one model call/turn): after_run_callback reads the WHOLE
     turn — user message + agent responses + tool results — and extracts
     durable facts worth remembering into the user's episodic memory. Capturing
     from the agent's own output + tool results (not just the user message) is
     the point: the durable knowledge an agent produces is exactly what a
     user-message-only capture would miss. Bounded by a timeout and fully
     swallowed, so it never breaks or hangs a run. Default on with the flag;
     disable with ADK_CC_MEMORY_AUTOCAPTURE=0.

Consolidation (episodic → semantic) runs out of band. It has two triggers that
form a HYBRID, both optional and both calling the same consolidate_user:
  - THRESHOLD (responsive, here): ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD=N promotes
    a user as soon as N unprocessed episodics stack up — checked right after a
    capture (the only moment the backlog grows). Deterministic, no model call.
  - PERIODIC (time-based): the scripts/memory_consolidator.py cron, or the
    in-process service/memory_scheduler.py loop — the staleness sweep + a
    safety net for users who never reach the threshold.
A shared lock (memory.consolidation_lock) serializes the in-process pair.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.utils.context_utils import Aclosing
from google.genai import types

from ..memory import (
    MemoryStore,
    consolidate_user,
    consolidation_lock,
    pending_episodic_count,
    recall_context,
)

_log = logging.getLogger(__name__)

_TENANT_KEY = "temp:tenant_context"
_DEFAULT_RECALL_BUDGET = 600
_DEFAULT_CAPTURE_TIMEOUT_S = 30
_DEFAULT_STALE_DAYS = 90
_MAX_FACTS = 6


def _consolidate_threshold() -> int:
    """Promote (episodic→semantic) once this many unprocessed episodics stack
    up for a user. 0/unset → no threshold trigger (rely on the scheduler/cron).
    This is the responsive half of the hybrid; the periodic scheduler
    (service/memory_scheduler.py) is the time-based sweep + straggler net."""
    try:
        return max(0, int(os.environ.get("ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD", "")))
    except ValueError:
        return 0


def _stale_days() -> int:
    try:
        return max(1, int(os.environ.get("ADK_CC_MEMORY_STALE_DAYS", "")))
    except ValueError:
        return _DEFAULT_STALE_DAYS


async def maybe_threshold_consolidate(store: MemoryStore, user_id: str):
    """If ≥ threshold unprocessed episodics have stacked for this user, run a
    deterministic (no-model) consolidation now. Serialized against the periodic
    scheduler via the shared lock; self-guarded so it never breaks a run."""
    try:
        threshold = _consolidate_threshold()
        if threshold <= 0:
            return None
        pending = pending_episodic_count(store, user_id)
        if pending < threshold:
            return None

        def _run():  # sync; runs in a worker thread, holds the cross-caller lock
            with consolidation_lock:
                return consolidate_user(store, user_id, stale_days=_stale_days())

        rep = await asyncio.to_thread(_run)
        _log.info(
            "memory: threshold consolidation user=%s pending=%d topics_consolidated=%d",
            user_id, pending, rep.topics_consolidated,
        )
        return rep
    except Exception as e:  # noqa: BLE001 — promotion must never break a run
        _log.warning(
            "memory: threshold consolidation skipped (%s: %s)", type(e).__name__, e
        )
        return None


def _capture_timeout() -> float:
    """Seconds to wait for the out-of-band capture extraction before giving up
    (swallowed). Configurable so a slow/rate-limited endpoint can be given more
    room without changing the production default."""
    try:
        return max(1.0, float(os.environ.get("ADK_CC_MEMORY_CAPTURE_TIMEOUT_S", "")))
    except ValueError:
        return _DEFAULT_CAPTURE_TIMEOUT_S

_CAPTURE_PROMPT = (
    "You maintain long-term memory for an AI assistant. Record ONLY durable "
    "facts about the USER and THEIR work — their identity and preferences, and "
    "their project's stack / config / decisions / outcomes. Good examples: "
    "\"user's name is X\", \"project deploys to Fly.io\", \"team chose "
    "Postgres\", \"user prefers dark mode\".\n"
    "Do NOT record: (a) general or domain knowledge, or facts about the SUBJECT "
    "MATTER being discussed — e.g. \"L2 caches are 256KB\", \"TAGE is a branch "
    "predictor\", \"DDR5 has 8 channels\" — those belong in documents, not user "
    "memory; (b) the user's questions, greetings, or one-off task steps. If a "
    "statement would be equally true for any user, it is NOT a user fact.\n\n"
    "Output one fact per line, EXACTLY:\n"
    "TOPIC: <2-5 word topic> | <one concise sentence>\n"
    "Only if there is genuinely nothing worth remembering, output: NONE\n\n"
    "TURN:\n{turn}"
)


def _recall_budget() -> int:
    try:
        return max(0, int(os.environ.get("ADK_CC_MEMORY_RECALL_BUDGET_TOKENS", "")))
    except ValueError:
        return _DEFAULT_RECALL_BUDGET


def _autocapture_enabled() -> bool:
    return os.environ.get("ADK_CC_MEMORY_AUTOCAPTURE") != "0"


def _tenant_user(state) -> tuple[str, str]:
    tc = state.get(_TENANT_KEY) if hasattr(state, "get") else None
    return (
        getattr(tc, "tenant_id", None) or "local",
        getattr(tc, "user_id", None) or "local",
    )


def _latest_user_text(contents) -> str:
    for content in reversed(list(contents or [])):
        if getattr(content, "role", None) != "user":
            continue
        text = _parts_text(getattr(content, "parts", None))
        if text:
            return text
    return ""


def _parts_text(parts) -> str:
    out = []
    for p in parts or []:
        if getattr(p, "thought", None):
            continue
        if getattr(p, "text", None) and p.text.strip():
            out.append(p.text.strip())
        fc = getattr(p, "function_call", None)
        if fc is not None:
            out.append(f"[called {getattr(fc, 'name', '?')}]")
        fr = getattr(p, "function_response", None)
        if fr is not None:
            resp = getattr(fr, "response", None)
            out.append(f"[result of {getattr(fr, 'name', '?')}: {str(resp)[:300]}]")
    return "\n".join(out)


def _turn_transcript(ictx: InvocationContext, *, max_chars: int = 6000) -> str:
    """User + agent + tool events for THIS invocation, as a compact transcript."""
    inv_id = ictx.invocation_id
    lines: list[str] = []
    for e in getattr(ictx.session, "events", None) or []:
        if getattr(e, "invocation_id", None) != inv_id:
            continue
        author = getattr(e, "author", "?")
        text = _parts_text(getattr(getattr(e, "content", None), "parts", None))
        if text:
            lines.append(f"{author}: {text}")
    blob = "\n".join(lines)
    return blob[-max_chars:]


def _parse_facts(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or s.upper() == "NONE":
            continue
        if s.upper().startswith("TOPIC:"):
            body = s[len("TOPIC:"):].strip()
            topic, sep, fact = body.partition("|")
            topic, fact = topic.strip(), fact.strip()
            if topic and fact:
                out.append((topic, fact))
    return out[:_MAX_FACTS]


class MemoryPlugin(BasePlugin):
    def __init__(self, *, name: str = "adk_cc_memory") -> None:
        super().__init__(name=name)

    # ---- recall injection (read) ----
    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        try:
            state = getattr(callback_context, "state", None)
            if state is None:
                state = getattr(getattr(callback_context, "session", None), "state", None)
            if state is None:
                return None
            tenant_id, user_id = _tenant_user(state)
            # Stamp the principal so the (shared, events-only) compaction
            # summarizer can reach this user's memory store when seeding the
            # summary (P3). Set before the empty-query return so it's available
            # even on turns with no user text.
            from ..memory import set_principal
            set_principal(tenant_id, user_id)
            query = _latest_user_text(getattr(llm_request, "contents", None))
            if not query:
                return None
            store = MemoryStore.for_tenant(tenant_id)
            block = recall_context(store, user_id, query, budget_tokens=_recall_budget())
            if block:
                _append_to_system_instruction(llm_request, block)
        except Exception as e:  # noqa: BLE001 — recall must never break a turn
            _log.warning("memory: recall skipped (%s: %s)", type(e).__name__, e)
        return None

    # ---- capture (write) ----
    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        if not _autocapture_enabled():
            return None
        ictx = invocation_context
        try:
            model = getattr(ictx.agent, "canonical_model", None)
            if model is None:
                return None
            transcript = _turn_transcript(ictx)
            if not transcript.strip():
                return None
            raw = await asyncio.wait_for(
                self._extract(model, transcript), timeout=_capture_timeout()
            )
            facts = _parse_facts(raw)
            if not facts:
                return None
            state = getattr(ictx.session, "state", None)
            tenant_id, user_id = _tenant_user(state)
            store = MemoryStore.for_tenant(tenant_id)
            sid = getattr(ictx.session, "id", None) or ""
            # Fix A+D: resolve each fact to an existing topic (corroborate/update)
            # or NEW, so drifted slugs fold at write time. Falls back to the
            # proposed slug on any model failure.
            from ..memory import resolve_facts
            resolutions = await resolve_facts(model, store, user_id, facts)
            for res in resolutions:
                store.add_episodic(user_id, res.fact, topic=res.topic,
                                   sources=[sid] if sid else None)
            _log.info("memory: captured %d fact(s) for user=%s", len(resolutions), user_id)
            # Hybrid promotion: this turn just grew the unprocessed backlog, so
            # this is the only moment it can cross the threshold — check here
            # (no need to poll on no-capture turns; the count only rises here).
            await maybe_threshold_consolidate(store, user_id)
        except asyncio.TimeoutError:
            _log.warning("memory: capture timed out (%ss)", _capture_timeout())
        except Exception as e:  # noqa: BLE001
            _log.warning("memory: capture skipped (%s: %s)", type(e).__name__, e)
        return None

    async def _extract(self, model, transcript: str) -> str:
        req = LlmRequest(
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part(text=_CAPTURE_PROMPT.format(turn=transcript))],
                )
            ],
            config=types.GenerateContentConfig(),
        )
        raw = ""
        async with Aclosing(model.generate_content_async(req, stream=False)) as agen:
            async for resp in agen:
                for p in (getattr(getattr(resp, "content", None), "parts", None) or []):
                    if not getattr(p, "thought", None) and getattr(p, "text", None):
                        raw += p.text
        return raw


def _append_to_system_instruction(req: LlmRequest, text: str) -> None:
    existing = req.config.system_instruction
    if existing is None:
        req.config.system_instruction = text
    elif isinstance(existing, str):
        req.config.system_instruction = existing + "\n\n" + text
    else:
        try:
            parts = list(existing) if isinstance(existing, list) else [existing]
            parts.append(types.Part(text=text))
            req.config.system_instruction = parts
        except Exception:
            pass
