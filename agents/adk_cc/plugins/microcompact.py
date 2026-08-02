"""Microcompaction: rewrite large tool-result content out of the request.

The request-rewriting tier of the context stack (context-defense plan,
corrected after the 2026-08-02 overflow incident). ADK's
EventsCompactionConfig only does whole-window LLM summarization; for a
coding agent, big tool outputs dominate the context. This plugin runs
`before_model_callback` and rewrites the OUTGOING request only — ADK
rebuilds `llm_request.contents` from session events each call, so stored
history and transcripts keep the full content.

TWO shapes are rewritten — the incident lived in the gap between them:

  - `function_response` parts of allowlisted tools (same-agent history).
    This shape was handled from day one, and it is WHY the transferred
    Explore agent never overflowed (its usage shrank 115k → 76k as old
    fetch results were stubbed).
  - TEXT parts matching ADK's foreign-tool-result rendering
    ("[Explore] `web_fetch` tool returned result: …" —
    `flows/llm_flows/contents.py _present_other_agent_message`). The SAME
    bytes microcompact was stubbing inside Explore's own requests re-entered
    the coordinator's request in this costume — ~833KB ≈ 289k tokens against
    a measured ~272k window — and the type-based filter missed them.

Rewrites prefer a cached LLM summary (`context/result_summaries` — findings
survive) and fall back to a mechanical stub (window survives). Idempotent:
already-rewritten parts are recognized and skipped.

ON by default since the incident. ADK_CC_MICROCOMPACT=0 disables the
always-on pass; the context guard still calls `rewrite_request` at its
reject line as the last resort regardless.
  ADK_CC_MICROCOMPACT_KEEP_RECENT   recent compactable results kept (default 4)
  ADK_CC_MICROCOMPACT_MIN_TOKENS    only rewrite results bigger than this (default 800)
  ADK_CC_RESULT_SUMMARIES           =0: stubs only, no summarizer calls
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin

from ..config.schema import env_bool
from ..context import result_summaries

_log = logging.getLogger(__name__)

# Tools whose results are large and safe to rewrite. Excludes small /
# interactive / stateful results (plan, wiki, ask_user_question, artifacts)
# where the content itself matters even when old.
_COMPACTABLE = frozenset({
    "run_bash", "read_file", "grep", "glob_files",
    "web_fetch", "web_search", "edit_file", "write_file",
})

_STUB_NOTE = "[old tool result cleared to save context — full content in session history]"
_SUMMARY_NOTE = "[condensed from {n} chars — full content in session history; re-run the tool if more is needed]"
_DEFAULT_KEEP_RECENT = 4
_DEFAULT_MIN_TOKENS = 800

# ADK renders ANOTHER agent's tool result as text:
#   "[Explore] `web_fetch` tool returned result: {...}"
# Matching that exact shape keeps rewriting away from genuine prose.
_FOREIGN_RESULT_RE = re.compile(
    r"^\[[^\]\n]{1,80}\] `[^`\n]{1,80}` tool returned result: ")
_FOREIGN_TOOL_RE = re.compile(r"`([^`\n]{1,80})`")


def _enabled() -> bool:
    # Default ON since the 2026-08-02 incident: the boundary shape made a
    # disabled-by-default rewriter a window-overflow footgun.
    return env_bool("ADK_CC_MICROCOMPACT", default=True)


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "")))
    except ValueError:
        return default


def _is_stub(response) -> bool:
    return isinstance(response, dict) and response.get("status") in (
        "cleared", "summarized")


def _is_rewritten_text(text: str) -> bool:
    return "[condensed from " in text[:400] or "chars evicted" in text[:400] \
        or _STUB_NOTE in text[:400]


def _est_tokens(response) -> int:
    """Rough token estimate of a tool-response payload (chars/4)."""
    try:
        return len(json.dumps(response, ensure_ascii=False, default=str)) // 4
    except Exception:
        return len(str(response)) // 4


async def rewrite_request(
    llm_request: LlmRequest,
    *,
    keep_recent: int,
    min_tokens: int,
    allow_summaries: bool = True,
    budget_tokens: Optional[int] = None,
) -> dict[str, Any]:
    """Rewrite oversized tool results in `llm_request` (both shapes), oldest
    first, keeping the newest `keep_recent` untouched. When `budget_tokens`
    is given, stop once that many tokens are freed (the guard's reject-line
    use); otherwise rewrite everything eligible (the always-on pass).
    Returns {"rewritten": n, "freed": tokens, "summarized": n}.
    """
    targets = []  # (part, kind, size_tokens)
    for content in llm_request.contents or []:
        for part in content.parts or []:
            fr = getattr(part, "function_response", None)
            if fr is not None:
                if (fr.name or "") not in _COMPACTABLE or _is_stub(fr.response):
                    continue
                targets.append((part, "response", _est_tokens(fr.response)))
                continue
            text = getattr(part, "text", None)
            if (text and len(text) >= min_tokens * 4
                    and _FOREIGN_RESULT_RE.match(text)
                    and not _is_rewritten_text(text)):
                targets.append((part, "text", len(text) // 4))

    if len(targets) <= keep_recent:
        return {"rewritten": 0, "freed": 0, "summarized": 0}

    old = targets[: len(targets) - keep_recent] if keep_recent else targets
    rewritten = freed = summarized = 0
    for part, kind, size in old:
        if size < min_tokens:
            continue
        if budget_tokens is not None and freed >= budget_tokens:
            break
        if kind == "response":
            fr = part.function_response
            raw = _payload_text(fr.response)
            summary = (await result_summaries.summarize(raw, tool=fr.name or "tool")
                       if allow_summaries else None)
            if summary:
                fr.response = {
                    "status": "summarized", "summary": summary,
                    "note": _SUMMARY_NOTE.format(n=len(raw)),
                }
                summarized += 1
                freed += max(0, size - _est_tokens(fr.response))
            else:
                fr.response = {"status": "cleared", "note": _STUB_NOTE}
                freed += size
        else:
            text = part.text
            m = _FOREIGN_TOOL_RE.search(text[:160])
            tool = m.group(1) if m else "tool"
            prefix_end = text.find(": ") + 2
            prefix = text[:prefix_end] if 0 < prefix_end < 200 else ""
            summary = (await result_summaries.summarize(text, tool=tool)
                       if allow_summaries else None)
            if summary:
                # Marker FIRST: it both flags the part as rewritten for
                # _is_rewritten_text and stops _FOREIGN_RESULT_RE matching
                # on later passes (idempotence for summaries of any length).
                part.text = (_SUMMARY_NOTE.format(n=len(text)) + "\n"
                             + prefix + summary)
                summarized += 1
                freed += max(0, (len(text) - len(part.text)) // 4)
            else:
                head = text[:200]
                part.text = (head + f"\n…[{len(text)} chars evicted to fit the "
                             "context window — re-run the tool if this is "
                             "still needed]")
                freed += max(0, (len(text) - len(part.text)) // 4)
        rewritten += 1

    if rewritten:
        _log.info(
            "microcompact: rewrote %d tool result(s) (%d summarized), ~%d tokens freed",
            rewritten, summarized, freed,
        )
        try:
            from .audit import emit_audit_event
            emit_audit_event({"event": "context_rewrite", "rewritten": rewritten,
                              "summarized": summarized, "freed_tokens": freed})
        except Exception:  # noqa: BLE001
            pass
    return {"rewritten": rewritten, "freed": freed, "summarized": summarized}


def _payload_text(response) -> str:
    try:
        return json.dumps(response, ensure_ascii=False, default=str)
    except Exception:
        return str(response)


class MicrocompactPlugin(BasePlugin):
    """Rewrite old, large tool-result content in the outgoing request."""

    def __init__(self, name: str = "adk_cc_microcompact") -> None:
        super().__init__(name=name)
        self._keep = _int_env("ADK_CC_MICROCOMPACT_KEEP_RECENT", _DEFAULT_KEEP_RECENT)
        self._min_tokens = _int_env("ADK_CC_MICROCOMPACT_MIN_TOKENS", _DEFAULT_MIN_TOKENS)
        if _enabled():
            _log.info(
                "MicrocompactPlugin: keep_recent=%d min_tokens=%d tools=%d summaries=%s",
                self._keep, self._min_tokens, len(_COMPACTABLE),
                result_summaries.enabled(),
            )

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        if not _enabled():
            return None
        try:
            await rewrite_request(
                llm_request, keep_recent=self._keep,
                min_tokens=self._min_tokens,
                allow_summaries=result_summaries.enabled(),
            )
        except Exception as e:  # noqa: BLE001 — never break a turn over an optimization
            _log.warning("microcompact skipped (%s: %s)", type(e).__name__, e)
        return None
