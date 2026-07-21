"""Microcompaction: evict large tool-result content before the model call.

The cheap tier of the compaction stack (master-plan P2, adapted from Claude
Code's microCompact). ADK's EventsCompactionConfig only does whole-window LLM
SUMMARIZATION; for a coding agent, big tool outputs (bash/read/grep) dominate the
context. This plugin runs `before_model_callback` and replaces the *content* of
old, large tool results in the OUTGOING request with a small stub — reclaiming
tokens at ZERO model cost, often keeping a session under the summarizer/reject
thresholds entirely.

Key properties:
  - Per-request only. ADK rebuilds `llm_request.contents` from the session events
    each turn, so this never mutates stored history — the full result stays in
    the session (and the transcript). The stub is applied fresh each turn.
  - Pairing-safe. Only `function_response.response` is replaced; the matching
    `function_call` part and the response's id/name are untouched, so the
    tool_use↔tool_result contract holds.
  - Keeps the most recent N compactable results verbatim, and anything below the
    size floor — recent/small results are what the model usually still needs.

Off by default. Enable with ADK_CC_MICROCOMPACT=1.
  ADK_CC_MICROCOMPACT_KEEP_RECENT   recent compactable results to keep (default 4)
  ADK_CC_MICROCOMPACT_MIN_TOKENS    only evict results bigger than this (default 800)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from ..config.schema import env_bool

_log = logging.getLogger(__name__)

# Tools whose results are large and safe to stub. Excludes small / interactive /
# stateful results (plan, wiki, ask_user_question, artifacts) where the content
# itself matters even when old.
_COMPACTABLE = frozenset({
    "run_bash", "read_file", "grep", "glob_files",
    "web_fetch", "web_search", "edit_file", "write_file",
})

_STUB_NOTE = "[old tool result cleared to save context — full content in session history]"
_DEFAULT_KEEP_RECENT = 4
_DEFAULT_MIN_TOKENS = 800


def _enabled() -> bool:
    return env_bool("ADK_CC_MICROCOMPACT")


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "")))
    except ValueError:
        return default


def _is_stub(response) -> bool:
    return isinstance(response, dict) and response.get("status") == "cleared" \
        and response.get("note") == _STUB_NOTE


def _est_tokens(response) -> int:
    """Rough token estimate of a tool-response payload (chars/4)."""
    try:
        return len(json.dumps(response, ensure_ascii=False, default=str)) // 4
    except Exception:
        return len(str(response)) // 4


class MicrocompactPlugin(BasePlugin):
    """Stub old, large tool-result content in the outgoing request."""

    def __init__(self, name: str = "adk_cc_microcompact") -> None:
        super().__init__(name=name)
        self._keep = _int_env("ADK_CC_MICROCOMPACT_KEEP_RECENT", _DEFAULT_KEEP_RECENT)
        self._min_tokens = _int_env("ADK_CC_MICROCOMPACT_MIN_TOKENS", _DEFAULT_MIN_TOKENS)
        if _enabled():
            _log.info(
                "MicrocompactPlugin: keep_recent=%d min_tokens=%d tools=%d",
                self._keep, self._min_tokens, len(_COMPACTABLE),
            )

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        if not _enabled():
            return None
        try:
            self._microcompact(llm_request)
        except Exception as e:  # noqa: BLE001 — never break a turn over an optimization
            _log.warning("microcompact skipped (%s: %s)", type(e).__name__, e)
        return None

    def _microcompact(self, llm_request: LlmRequest) -> None:
        # Collect compactable function_response parts in conversation order.
        targets = []  # list of (part, response)
        for content in llm_request.contents or []:
            for part in content.parts or []:
                fr = getattr(part, "function_response", None)
                if fr is None:
                    continue
                if (fr.name or "") not in _COMPACTABLE:
                    continue
                targets.append((part, fr))

        if len(targets) <= self._keep:
            return  # nothing old enough to evict

        # Protect the most recent N; consider the rest for eviction.
        old = targets[: len(targets) - self._keep] if self._keep else targets
        evicted = freed = 0
        for part, fr in old:
            resp = fr.response
            if _is_stub(resp):
                continue
            size = _est_tokens(resp)
            if size < self._min_tokens:
                continue  # small enough to keep verbatim
            fr.response = {"status": "cleared", "note": _STUB_NOTE}
            evicted += 1
            freed += size

        if evicted:
            # INFO: this is an infrequent, meaningful action (like memory
            # capture / consolidation logging), and the per-request token
            # estimators don't count function_response payloads — so this log
            # line is the authoritative signal that microcompaction fired.
            _log.info(
                "microcompact: evicted %d tool result(s), ~%d tokens freed",
                evicted, freed,
            )
