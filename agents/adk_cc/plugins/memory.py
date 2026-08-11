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
import re
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from ..config.schema import env_bool, as_int
from ..memory import (
    MemoryStore,
    consolidate_user,
    consolidation_lock,
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
        return max(0, as_int(os.environ.get("ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD", "")))
    except ValueError:
        return 0


def _stale_days() -> int:
    try:
        return max(1, as_int(os.environ.get("ADK_CC_MEMORY_STALE_DAYS", "")))
    except ValueError:
        return _DEFAULT_STALE_DAYS


def _threshold_synthesizer(model):
    """Synthesizer for the threshold trigger. LLM (genuinely rewrites the
    captures into a distilled fact) when a model is available and
    ADK_CC_MEMORY_SYNTH != 'deterministic' — same convention as the periodic
    scheduler. Else None → deterministic latest-wins (semantic text == newest
    episodic verbatim).

    NB: ADK_CC_MEMORY_SYNTH only governs THIS consolidation-synthesis step. The
    capture path still calls the model to EXTRACT facts (unless
    ADK_CC_MEMORY_AUTOCAPTURE=0) and to RESOLVE topics (unless
    ADK_CC_MEMORY_RESOLVE=0); a fully model-free memory hot path needs all
    three flags, not SYNTH alone."""
    if model is None or os.environ.get("ADK_CC_MEMORY_SYNTH") == "deterministic":
        return None
    try:
        from ..memory.synth import make_llm_synthesizer
        return make_llm_synthesizer(model)
    except Exception:  # noqa: BLE001
        return None


async def maybe_threshold_consolidate(store: MemoryStore, user_id: str, model=None):
    """Promote topics whose unprocessed episodic count reached the threshold
    (episodic→semantic). PER-TOPIC: the threshold expresses corroboration —
    "this fact was observed N times" — so a lone capture on topic A must not
    ride to semantic on the back of unrelated captures B, C, D (which is what
    a global pending count did: 4 singleton topics ≥ 2 → everything promoted
    at confidence 0.5). Topics below the bar stay episodic until corroborated
    or until the periodic scheduler's sweep promotes them time-based. Uses the
    LLM synthesizer unless ADK_CC_MEMORY_SYNTH=deterministic. Serialized
    against the periodic scheduler via the shared lock; self-guarded so it
    never breaks a run."""
    try:
        threshold = _consolidate_threshold()
        if threshold <= 0:
            return None
        from ..memory.store import ACTIVE, DRAFT
        from ..memory.consolidate import _canonical_map, _default_canonicalizer

        pending = [i for i in store.list_episodic(user_id)
                   if i.status in (ACTIVE, DRAFT)]
        # #129-4: count by CANONICAL topic — the capture LLM mints slug
        # variants for one fact ('preferred-language' /
        # 'user-preferred-language'), and exact counting kept each variant
        # forever below the corroboration bar.
        mapping = _canonical_map(_default_canonicalizer(),
                                 [i.topic for i in pending], [])
        per_topic: dict[str, int] = {}
        for item in pending:
            t = mapping.get(item.topic, item.topic)
            per_topic[t] = per_topic.get(t, 0) + 1
        eligible = {t for t, n in per_topic.items() if n >= threshold}
        if not eligible:
            return None

        synth = _threshold_synthesizer(model)

        def _run():  # sync; runs in a worker thread, holds the cross-caller lock
            with consolidation_lock:
                return consolidate_user(
                    store, user_id, synthesizer=synth,
                    stale_days=_stale_days(), only_topics=eligible)

        rep = await asyncio.to_thread(_run)
        _log.info(
            "memory: threshold consolidation user=%s topics=%d/%d pending "
            "synth=%s", user_id, rep.topics_consolidated, len(per_topic),
            "llm" if synth else "deterministic",
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

# The capture scope must MATCH the store scope, or recall contaminates
# (#126 review): on DESKTOP memory is per-PROJECT, so project stack/config/
# decisions belong; on WEB it is per-USER across unrelated sessions, so
# only cross-session durable facts qualify and session-scoped work details
# (current files, in-progress task state, this conversation's artifacts)
# must be excluded — they would be recalled into every future session.
_CAPTURE_SCOPE_PROJECT = (
    "Record ONLY durable facts about the USER and THIS PROJECT — the user's "
    "identity and preferences, and the project's stack / config / decisions "
    "/ outcomes. Good examples: \"user's name is X\", \"project deploys to "
    "Fly.io\", \"team chose Postgres\", \"user prefers dark mode\"."
)
_CAPTURE_SCOPE_USER = (
    "Record ONLY durable facts about the USER that will still be true and "
    "useful in their FUTURE, UNRELATED conversations — identity, role, "
    "standing preferences, long-lived constraints. Good examples: \"user's "
    "name is X\", \"user prefers answers in Korean\", \"user works with "
    "pandas 2.x only\".\n"
    "Do NOT record anything scoped to THIS session's work: current file "
    "names/paths, the task being worked on, choices made for this "
    "conversation's artifacts — those die with the session."
)
_CAPTURE_PROMPT = (
    "You maintain long-term memory for an AI assistant. {scope}\n"
    "Do NOT record: (a) general or domain knowledge, or facts about the SUBJECT "
    "MATTER being discussed — e.g. \"L2 caches are 256KB\", \"TAGE is a branch "
    "predictor\", \"DDR5 has 8 channels\" — those belong in documents, not user "
    "memory; (b) the user's questions, greetings, or one-off task steps. Two "
    "tests: if a statement would be equally true for any user, it is NOT a "
    "user fact; if it will not matter in the user's next, unrelated "
    "conversation, it is NOT durable.\n\n"
    "Output one fact per line, EXACTLY:\n"
    "TOPIC: <2-5 word topic> | <one concise sentence>\n"
    "Only if there is genuinely nothing worth remembering, output: NONE\n\n"
    "TURN:\n{turn}"
)


def _notes_autocapture() -> bool:
    return env_bool("ADK_CC_SESSION_NOTES_AUTOCAPTURE", False)


def _capture_prompt(turn: str, known_topics: Optional[list[str]] = None) -> str:
    from .. import deployment

    scope = (_CAPTURE_SCOPE_PROJECT if deployment.is_desktop()
             else _CAPTURE_SCOPE_USER)
    if _notes_autocapture() and not deployment.is_desktop():
        # #127 P1: route session-scoped facts into SESSION NOTES instead of
        # dropping them — still excluded from user memory.
        scope += ("\nEXCEPTION: if a fact matters only for THIS session's "
                  "work (a decision, a discovered constraint, task state), "
                  "output it as:\nSESSION: <one concise sentence>")
    if known_topics:
        # #129-4b: bias proposed slugs toward EXISTING topics at the source —
        # a fresh slug for a known fact litters the episodic tier and leans
        # on the resolver/canonicalizer to clean up after the fact.
        scope += ("\nKNOWN TOPICS: " + ", ".join(known_topics)
                  + "\nWhen a fact corroborates or updates one of these, "
                    "REUSE that exact slug as its topic; if it is already "
                    "recorded there verbatim, do not output it at all.")
    return _CAPTURE_PROMPT.format(scope=scope, turn=turn)


_KNOWN_TOPICS_CAP = 40


def _known_topics(store: MemoryStore, user_id: str) -> list[str]:
    """The user's existing topic slugs (semantic first — the durable ones —
    then pending episodic), capped so the capture prompt stays small."""
    from ..memory.store import ACTIVE, CONSOLIDATED, DRAFT

    out: dict[str, None] = {}
    try:
        for s in store.list_semantic(user_id):
            if s.status in (ACTIVE, CONSOLIDATED):
                out.setdefault(s.topic)
        for e in store.list_episodic(user_id):
            if e.status in (ACTIVE, DRAFT):
                out.setdefault(e.topic)
    except Exception:  # noqa: BLE001 — a hint, never a blocker
        pass
    return list(out)[:_KNOWN_TOPICS_CAP]


def _recall_budget() -> int:
    try:
        return max(0, as_int(os.environ.get("ADK_CC_MEMORY_RECALL_BUDGET_TOKENS", "")))
    except ValueError:
        return _DEFAULT_RECALL_BUDGET


def _autocapture_enabled() -> bool:
    return env_bool("ADK_CC_MEMORY_AUTOCAPTURE", True)


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
    """Parse `TOPIC: <slug> | <fact>` lines. Hardened against glued model
    output (a mid-line `TOPIC:` starts a new entry — seen live when a
    double-yielding delegate joined two copies without a newline) and against
    duplicate facts (same topic+text kept once)."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or s.upper() == "NONE":
            continue
        for chunk in re.split(r"(?=TOPIC:)", s, flags=re.IGNORECASE):
            c = chunk.strip()
            if not c.upper().startswith("TOPIC:"):
                continue
            body = c[len("TOPIC:"):].strip()
            topic, _sep, fact = body.partition("|")
            topic, fact = topic.strip(), fact.strip()
            key = (topic.lower(), fact)
            if topic and fact and key not in seen:
                seen.add(key)
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
            # recall scans/parses the user's memory files — offload so this
            # per-turn read never blocks the event loop / health checks.
            block = await asyncio.to_thread(
                recall_context, store, user_id, query, budget_tokens=_recall_budget())
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
            # Observability (found live: 1-of-11 turns captured with zero
            # trace of why the others didn't): every skip path logs its
            # reason at INFO — one terse line per turn.
            model = getattr(ictx.agent, "canonical_model", None)
            if model is None:
                _log.info("memory: capture skipped (no model)")
                return None
            transcript = _turn_transcript(ictx)
            if not transcript.strip():
                _log.info("memory: capture skipped (empty transcript)")
                return None
            state = getattr(ictx.session, "state", None)
            tenant_id, user_id = _tenant_user(state)
            store = MemoryStore.for_tenant(tenant_id)
            # #129-4b: existing slugs go INTO the capture prompt so the
            # extractor reuses them instead of minting variants.
            known = await asyncio.to_thread(_known_topics, store, user_id)
            raw = await asyncio.wait_for(
                self._extract(model, transcript, known_topics=known),
                timeout=_capture_timeout(),
            )
            facts = _parse_facts(raw)
            session_lines = [l[len("SESSION:"):].strip()
                             for l in (raw or "").splitlines()
                             if l.strip().upper().startswith("SESSION:")]
            if session_lines and _notes_autocapture():
                await self._append_session_notes(ictx, session_lines)
            if not facts:
                _log.info(
                    "memory: capture found no durable facts (raw %d chars: %r)",
                    len(raw or ""), (raw or "")[:80])
                return None
            sid = getattr(ictx.session, "id", None) or ""
            # Fix A+D: resolve each fact to an existing topic (corroborate/update)
            # or NEW, so drifted slugs fold at write time. Falls back to the
            # proposed slug on any model failure.
            from ..memory import resolve_facts
            resolutions = await resolve_facts(model, store, user_id, facts)

            def _persist() -> None:
                for res in resolutions:
                    store.add_episodic(user_id, res.fact, topic=res.topic,
                                       sources=[sid] if sid else None)

            # The episodic writes (put_doc + changelog append per fact) are
            # offloaded so the capture path doesn't occupy the loop after the run.
            await asyncio.to_thread(_persist)
            _log.info("memory: captured %d fact(s) for user=%s", len(resolutions), user_id)
            # Hybrid promotion: this turn just grew the unprocessed backlog, so
            # this is the only moment it can cross the threshold — check here
            # (no need to poll on no-capture turns; the count only rises here).
            # Pass the model so consolidation can LLM-synthesize the semantic
            # fact (unless ADK_CC_MEMORY_SYNTH=deterministic).
            await maybe_threshold_consolidate(store, user_id, model=model)
        except asyncio.TimeoutError:
            _log.warning("memory: capture timed out (%ss)", _capture_timeout())
        except Exception as e:  # noqa: BLE001
            _log.warning("memory: capture skipped (%s: %s)", type(e).__name__, e)
        return None

    async def _append_session_notes(self, ictx, lines: list) -> None:
        """#127 P1: fold capture-routed session facts into the notes state
        via a state-delta event (the session_title precedent) — the notes
        block then re-injects them next turn."""
        try:
            from google.adk.events.event import Event
            from google.adk.events.event_actions import EventActions

            from ..tools.session_notes import STATE_KEY, notes_budget_chars

            state = getattr(ictx.session, "state", None) or {}
            existing = str(state.get(STATE_KEY) or "").rstrip()
            add = "\n".join(f"- {l}" for l in lines if l)
            merged = (existing + "\n\n" + add).strip() if existing else add
            cap = notes_budget_chars()
            if len(merged) > cap:
                merged = merged[-cap:]
            service = getattr(ictx, "session_service", None)
            if service is None:
                return
            await service.append_event(
                ictx.session,
                Event(invocation_id=ictx.invocation_id, author=self.name,
                      actions=EventActions(state_delta={STATE_KEY: merged})),
            )
            _log.info("memory: routed %d session-scoped fact(s) into notes",
                      len(lines))
        except Exception as e:  # noqa: BLE001
            _log.warning("session-notes routing skipped (%s: %s)",
                         type(e).__name__, e)

    async def _extract(self, model, transcript: str,
                       known_topics: Optional[list[str]] = None) -> str:
        req = LlmRequest(
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part(
                        text=_capture_prompt(transcript, known_topics))],
                )
            ],
            config=types.GenerateContentConfig(),
        )
        # double-yield-safe collector (see memory/llm_text.py)
        from ..memory.llm_text import final_response_text
        return await final_response_text(model, req)


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
