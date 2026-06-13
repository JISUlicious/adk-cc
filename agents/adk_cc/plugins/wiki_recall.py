"""Wiki recall + auto-capture plugin (ADK_CC_WIKI=1).

Two cross-cutting behaviors the agent shouldn't have to remember to invoke:

  1. RECALL (read, cheap — no model call): on `before_model_callback`,
     inject a SMALL token-budgeted slice of the wiki relevant to the user's
     latest message into the system_instruction — the index, top pages
     (shared wiki + the caller's private notes, tagged by scope), and any
     read-time discrepancy between the two. This is the Hermes "tiny
     always-injected memory" surface. Appended AFTER project context so the
     stable context sits first and this turn-volatile recall sits last.

  2. AUTO-CAPTURE (write, one model call per turn — OPT-IN via
     ADK_CC_WIKI_AUTOCAPTURE=1): spawn-early/persist-late, exactly like
     SessionTitlePlugin. `before_run` spawns an out-of-band extraction of
     any durable fact the user asserted (concurrent with the turn);
     `after_run` awaits it and writes it to the caller's PRIVATE inbox. The
     extractor returns NONE for questions/commands/chit-chat, so only
     genuine knowledge lands. It's a per-turn model call, hence opt-in;
     recall injection alone (behavior 1) is free and on by default.

Everything is wrapped: recall/capture failures log and swallow, never
breaking the run. Scope resolves from `temp:tenant_context`, degrading to
local/local on the flat dev path.
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

from ..memory import WikiStore
from ..memory import search as searchlib

_log = logging.getLogger(__name__)

_TENANT_KEY = "temp:tenant_context"
_DEFAULT_BUDGET = 800
_MAX_PENDING = 32  # leak guard for the capture-task map (see session_title)

_CAPTURE_PROMPT = (
    "You are a memory extractor for a knowledge wiki. Read the user's "
    "message below. If it ASSERTS a durable, factual piece of knowledge "
    "worth remembering later (a definition, a decision, a spec, a stable "
    "fact about the user's project or domain), output EXACTLY two lines:\n"
    "TOPIC: <2-5 word topic>\n"
    "<one or two sentence fact, in your own words>\n\n"
    "If the message is a question, a request to do work, a command, "
    "chit-chat, or otherwise carries no durable fact, output EXACTLY:\n"
    "NONE\n\n"
    "User message:\n{user}"
)


def _budget_tokens() -> int:
    try:
        return max(0, int(os.environ.get("ADK_CC_WIKI_RECALL_BUDGET_TOKENS", "")))
    except ValueError:
        return _DEFAULT_BUDGET


def _autocapture_enabled() -> bool:
    return os.environ.get("ADK_CC_WIKI_AUTOCAPTURE") == "1"


def _tenant_user_from_state(state) -> tuple[str, str]:
    tc = state.get(_TENANT_KEY) if hasattr(state, "get") else None
    tenant_id = getattr(tc, "tenant_id", None) or "local"
    user_id = getattr(tc, "user_id", None) or "local"
    return tenant_id, user_id


def _latest_user_text(contents) -> str:
    """Last user-authored text in the request's contents, or ''."""
    for content in reversed(list(contents or [])):
        if getattr(content, "role", None) != "user":
            continue
        parts = getattr(content, "parts", None) or []
        text = "\n".join(
            p.text.strip()
            for p in parts
            if not getattr(p, "thought", None)
            and getattr(p, "text", None)
            and p.text.strip()
        )
        if text:
            return text
    return ""


def _append_to_system_instruction(req: LlmRequest, text: str) -> None:
    """Append `text` to system_instruction across its None/str/Part/list
    shapes (mirrors task_reminder). Recall is turn-volatile → appended last."""
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


def _parse_capture(raw: str) -> Optional[tuple[str, str]]:
    """Parse the extractor reply into (topic, fact), or None for NONE / junk."""
    s = (raw or "").strip()
    if not s or s.upper().startswith("NONE"):
        return None
    topic = ""
    fact_lines: list[str] = []
    for line in s.splitlines():
        stripped = line.strip()
        if not topic and stripped.upper().startswith("TOPIC:"):
            topic = stripped[len("TOPIC:"):].strip()
            continue
        if stripped:
            fact_lines.append(stripped)
    fact = " ".join(fact_lines).strip()
    if not topic or not fact:
        return None
    return topic, fact


class WikiRecallPlugin(BasePlugin):
    """Injects budgeted wiki recall every turn and (opt-in) auto-captures
    durable user-asserted facts into the caller's inbox."""

    def __init__(self, *, name: str = "adk_cc_wiki_recall") -> None:
        super().__init__(name=name)
        # invocation_id -> in-flight capture extraction (spawn→persist).
        self._pending: dict[str, asyncio.Task] = {}

    # ---- recall injection (read; cheap; on by default) -------------------
    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        try:
            session = getattr(callback_context, "session", None)
            state = getattr(session, "state", None)
            if state is None:
                return None
            tenant_id, user_id = _tenant_user_from_state(state)
            query = _latest_user_text(getattr(llm_request, "contents", None))
            if not query:
                return None
            store = WikiStore.for_tenant(tenant_id)
            if not os.path.isdir(store.wiki_dir):
                return None  # nothing compiled yet — inject nothing
            block = searchlib.recall_context(
                store, query, user_id=user_id, budget_tokens=_budget_tokens()
            )
            if block:
                _append_to_system_instruction(
                    llm_request,
                    "# Knowledge wiki (recalled for this turn)\n" + block,
                )
        except Exception as e:  # noqa: BLE001 — recall must never break a turn
            _log.warning("wiki_recall: inject skipped (%s: %s)", type(e).__name__, e)
        return None

    # ---- auto-capture: spawn (Step 1, opt-in) ----------------------------
    async def before_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> Optional[types.Content]:
        if not _autocapture_enabled():
            return None
        try:
            ictx = invocation_context
            user_text = self._content_text(ictx.user_content)
            if not user_text:
                return None
            model = getattr(ictx.agent, "canonical_model", None)
            if model is None:
                return None
            if len(self._pending) >= _MAX_PENDING:
                self._pending.clear()
            prompt = _CAPTURE_PROMPT.format(user=user_text[:4000])
            self._pending[ictx.invocation_id] = asyncio.create_task(
                self._extract(model, prompt)
            )
        except Exception as e:  # noqa: BLE001
            _log.warning("wiki_recall: capture spawn skipped (%s: %s)", type(e).__name__, e)
        return None

    # ---- auto-capture: persist (Step 4) ----------------------------------
    async def after_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        ictx = invocation_context
        task = self._pending.pop(ictx.invocation_id, None)
        if task is None:
            return
        try:
            parsed = await task
            if not parsed:
                return
            topic, fact = parsed
            state = getattr(ictx.session, "state", None)
            tenant_id, user_id = _tenant_user_from_state(state)
            store = WikiStore.for_tenant(tenant_id).ensure()
            doc = store.add_inbox(user_id, fact, topic=topic)
            _log.info(
                "wiki_recall: captured %r → inbox/%s (user=%s)",
                topic, doc.doc_id, user_id,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning("wiki_recall: capture persist skipped (%s: %s)", type(e).__name__, e)

    # ---- internals -------------------------------------------------------
    @staticmethod
    def _content_text(content: Optional[types.Content]) -> str:
        parts = getattr(content, "parts", None) or []
        return "\n".join(
            p.text.strip()
            for p in parts
            if not getattr(p, "thought", None)
            and getattr(p, "text", None)
            and p.text.strip()
        )

    async def _extract(self, model, prompt: str) -> Optional[tuple[str, str]]:
        """Out-of-band extraction. Returns (topic, fact) or None on NONE/fail."""
        try:
            req = LlmRequest(
                contents=[
                    types.Content(role="user", parts=[types.Part(text=prompt)])
                ],
                config=types.GenerateContentConfig(),
            )
            raw = ""
            async with Aclosing(
                model.generate_content_async(req, stream=False)
            ) as agen:
                async for resp in agen:
                    content = getattr(resp, "content", None)
                    for p in (getattr(content, "parts", None) or []):
                        if not getattr(p, "thought", None) and getattr(p, "text", None):
                            raw += p.text
            return _parse_capture(raw)
        except Exception as e:  # noqa: BLE001
            _log.warning("wiki_recall: extraction failed (%s: %s)", type(e).__name__, e)
            return None
