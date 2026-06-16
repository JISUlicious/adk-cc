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

Consolidation (episodic → semantic) is a separate cron (scripts/
memory_consolidator.py), per the background-cron design choice.
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

from ..memory import MemoryStore, recall_context

_log = logging.getLogger(__name__)

_TENANT_KEY = "temp:tenant_context"
_DEFAULT_RECALL_BUDGET = 600
_CAPTURE_TIMEOUT_S = 30
_MAX_FACTS = 6

_CAPTURE_PROMPT = (
    "You maintain long-term memory for an AI assistant. From the turn below, "
    "list facts worth remembering for FUTURE conversations: the user's "
    "identity and preferences, their project's stack / config / decisions, and "
    "durable outcomes. Examples of durable facts: \"user's name is X\", "
    "\"project deploys to Fly.io\", \"team chose Postgres\", \"user prefers "
    "dark mode\". Do NOT record the user's questions, greetings, or one-off "
    "task steps.\n\n"
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
                self._extract(model, transcript), timeout=_CAPTURE_TIMEOUT_S
            )
            facts = _parse_facts(raw)
            if not facts:
                return None
            state = getattr(ictx.session, "state", None)
            tenant_id, user_id = _tenant_user(state)
            store = MemoryStore.for_tenant(tenant_id)
            sid = getattr(ictx.session, "id", None) or ""
            for topic, fact in facts:
                store.add_episodic(user_id, fact, topic=topic, sources=[sid] if sid else None)
            _log.info("memory: captured %d fact(s) for user=%s", len(facts), user_id)
        except asyncio.TimeoutError:
            _log.warning("memory: capture timed out (%ss)", _CAPTURE_TIMEOUT_S)
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
