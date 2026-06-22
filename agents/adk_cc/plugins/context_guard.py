"""Pre-flight context-length guardrail.

ADK ships a complete post-invocation compaction system
(`google.adk.apps.compaction` + `EventsCompactionConfig` +
`LlmEventSummarizer`) — wire that as the primary defense.

This plugin runs `before_model_callback` and adds the case ADK can't
cover: pre-flight WARN logging and fail-soft REJECT for the rare turn
that would jump from below threshold to over the model's window in a
single step (e.g. a tool returning an unexpectedly large payload).
ADK's compaction is reactive; this is preventive.

Two interventions only — no trim, no LLM call. Both delegated to ADK:

  - **WARN** (default 75% of `ADK_CC_MAX_CONTEXT_TOKENS`): structured
    log line so observability picks it up. Telemetry only.
  - **REJECT** (default 95%): return an early `LlmResponse` with a
    "context near full" message. Catches the request before it would
    otherwise 500 against the model server.

Disabled gracefully when `ADK_CC_MAX_CONTEXT_TOKENS` is unset — the
plugin attaches but does nothing. Plugin-chain wiring stays uniform
across deployments.

Token counting: uses the shared `estimate_prompt_tokens` helper
(`adk_cc/permissions/token_counter.py`) which mirrors ADK's
`_latest_prompt_token_count` algorithm — prefers the model's own
`usage_metadata.prompt_token_count` from session events when
available, falls back to chars/4 across `llm_request.contents`.
Same algorithm ADK's `EventsCompactionConfig` uses for its
threshold check, so the two layers can no longer disagree.

A separate `litellm.token_counter` reading is computed when the
plugin's logger is at DEBUG, for diagnostic comparison only — useful
when investigating "ADK didn't compact but the plugin REJECTs" /
vice-versa reports. The threshold decisions themselves use the
shared estimator exclusively.
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

from ..permissions.token_counter import (
    estimate_prompt_tokens,
    estimate_prompt_tokens_full,
)

_log = logging.getLogger(__name__)

_REJECT_TEXT = (
    "This session's context is near full. Please summarize key findings "
    "and start a fresh session."
)


class ContextGuardPlugin(BasePlugin):
    """WARN at threshold, REJECT at hard limit. ADK compaction does the rest."""

    def __init__(self, name: str = "adk_cc_context_guard") -> None:
        super().__init__(name=name)

        max_str = os.environ.get("ADK_CC_MAX_CONTEXT_TOKENS")
        self._max: Optional[int] = int(max_str) if max_str else None

        if self._max is None:
            self._warn = None
            self._reject = None
            return

        # Reserve output headroom for the response (and any compaction summary)
        # so WARN/REJECT trigger BEFORE the window is truly full — CC reserves
        # min(model_max_output, 20k). Opt-in (default 0 preserves prior
        # behavior); derived WARN/REJECT are computed off the EFFECTIVE window.
        reserve_str = os.environ.get("ADK_CC_CONTEXT_RESERVE_TOKENS")
        self._reserve = int(reserve_str) if reserve_str else 0
        self._effective = max(1, self._max - self._reserve)

        warn_str = os.environ.get("ADK_CC_CONTEXT_WARN_TOKENS")
        reject_str = os.environ.get("ADK_CC_CONTEXT_REJECT_TOKENS")
        self._warn = int(warn_str) if warn_str else int(self._effective * 0.75)
        self._reject = int(reject_str) if reject_str else int(self._effective * 0.95)

        # Opt-in: count function_call/function_response payloads in the REJECT
        # decision (the ADK-consistent estimator ignores them, under-counting
        # tool-heavy turns — a real REJECT-safety gap). Default off to preserve
        # ADK-aligned behavior; the fuller number is always shown in the logs.
        self._count_tool_payloads = (
            os.environ.get("ADK_CC_CONTEXT_COUNT_TOOL_PAYLOADS") == "1"
        )

        # Logged at startup so operators see the resolved ladder and can catch
        # typos / misordering immediately.
        _log.info(
            "ContextGuardPlugin: MAX=%d RESERVE=%d EFFECTIVE=%d WARN=%d REJECT=%d "
            "count_tool_payloads=%s",
            self._max, self._reserve, self._effective, self._warn, self._reject,
            self._count_tool_payloads,
        )
        self._validate_ladder()

    def _validate_ladder(self) -> None:
        """Sanity-check the warn→compact→reject→effective ordering and log a
        WARN on misconfiguration (the compaction trigger should fire before our
        WARN so summarization is the backstop ahead of REJECT)."""
        if not (self._warn < self._reject <= self._effective):
            _log.warning(
                "ContextGuardPlugin ladder looks misordered: expected "
                "WARN(%d) < REJECT(%d) <= EFFECTIVE(%d)",
                self._warn, self._reject, self._effective,
            )
        thr = os.environ.get("ADK_CC_COMPACTION_TOKEN_THRESHOLD")
        if thr:
            try:
                thr_i = int(thr)
            except ValueError:
                return
            if thr_i >= self._warn:
                _log.warning(
                    "ContextGuardPlugin: ADK_CC_COMPACTION_TOKEN_THRESHOLD=%d is "
                    ">= WARN=%d — compaction may not fire before the WARN/REJECT "
                    "ladder. Set the threshold below WARN so summarization is the "
                    "backstop.", thr_i, self._warn,
                )

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        if self._max is None:
            return None  # disabled

        session_events = self._session_events(callback_context)
        base = estimate_prompt_tokens(llm_request, session_events=session_events)
        # Payload-inclusive estimate (counts tool args/results the ADK-consistent
        # count ignores). Used for the decision only when opted in; always shown.
        full = estimate_prompt_tokens_full(llm_request, session_events=session_events)
        tokens = full if self._count_tool_payloads else base
        ratio = tokens / self._effective if self._effective else 0.0

        # Diagnostic-only: when DEBUG is on, also compute the
        # litellm-based count so operators investigating an
        # "ADK didn't compact but plugin REJECTs" / vice-versa report
        # can see both numbers side-by-side. Threshold decisions
        # below use the shared estimator only.
        if _log.isEnabledFor(logging.DEBUG):
            litellm_tokens = self._count_tokens_via_litellm(llm_request)
            _log.debug(
                "ContextGuardPlugin counts: shared=%d litellm=%d delta=%d",
                tokens,
                litellm_tokens,
                litellm_tokens - tokens,
                extra={
                    "shared_estimate": tokens,
                    "litellm_count": litellm_tokens,
                    "delta": litellm_tokens - tokens,
                },
            )

        if tokens >= self._reject:
            session_id = self._session_id(callback_context)
            _log.warning(
                "ContextGuardPlugin REJECT: tokens=%d (base=%d full=%d) "
                "effective=%d ratio=%.2f session_id=%s",
                tokens, base, full, self._effective, ratio, session_id,
            )
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=_REJECT_TEXT)],
                ),
            )

        if tokens >= self._warn:
            session_id = self._session_id(callback_context)
            _log.warning(
                "ContextGuardPlugin WARN: tokens=%d (base=%d full=%d) "
                "effective=%d ratio=%.2f session_id=%s",
                tokens, base, full, self._effective, ratio, session_id,
            )

        return None

    def _count_tokens_via_litellm(self, llm_request: LlmRequest) -> int:
        """Per-model accurate count via litellm; chars/4 fallback on
        failure. Used for the DEBUG comparison log line only —
        threshold decisions use the shared estimator that agrees with
        ADK's compaction counter."""
        messages = self._to_messages(llm_request)
        model = self._model_id(llm_request)
        try:
            import litellm

            return int(litellm.token_counter(model=model, messages=messages))
        except Exception:
            joined = "\n".join(m.get("content", "") for m in messages if isinstance(m.get("content"), str))
            return len(joined) // 4

    @staticmethod
    def _session_events(callback_context: CallbackContext) -> list:
        """Best-effort session-event fetch for the
        `usage_metadata.prompt_token_count` lookup. Returns an empty
        list when the context, session, or events are unavailable —
        the estimator then falls straight to chars/4 over
        llm_request.contents."""
        try:
            session = getattr(callback_context, "session", None)
            if session is None:
                return []
            events = getattr(session, "events", None)
            if not events:
                return []
            return list(events)
        except Exception:
            return []

    def _to_messages(self, llm_request: LlmRequest) -> list[dict]:
        """Flatten ADK's LlmRequest into LiteLLM-style messages."""
        msgs: list[dict] = []

        # System instruction first.
        si = getattr(llm_request.config, "system_instruction", None) if llm_request.config else None
        if si is not None:
            si_text = self._extract_text(si)
            if si_text:
                msgs.append({"role": "system", "content": si_text})

        # Then conversation contents.
        for content in llm_request.contents or []:
            role = content.role or "user"
            if role == "model":
                role = "assistant"
            text_parts: list[str] = []
            for p in content.parts or []:
                if getattr(p, "text", None):
                    text_parts.append(p.text)
                fc = getattr(p, "function_call", None)
                if fc is not None:
                    text_parts.append(f"[function_call:{fc.name}({fc.args})]")
                fr = getattr(p, "function_response", None)
                if fr is not None:
                    text_parts.append(f"[function_response:{fr.name}={fr.response}]")
            if text_parts:
                msgs.append({"role": role, "content": "\n".join(text_parts)})

        return msgs

    @staticmethod
    def _extract_text(value) -> str:
        """system_instruction may be str | list[Part] | Part."""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(
                p.text for p in value if getattr(p, "text", None)
            )
        if getattr(value, "text", None):
            return value.text
        return ""

    @staticmethod
    def _model_id(llm_request: LlmRequest) -> str:
        """Best-effort model id for the tokenizer. Falls back to env."""
        model = getattr(llm_request, "model", None)
        if model:
            return model
        return os.environ.get("ADK_CC_MODEL", "gpt-4")

    @staticmethod
    def _session_id(callback_context: CallbackContext) -> str:
        try:
            session = callback_context.session
            return getattr(session, "id", "") or "?"
        except Exception:
            return "?"
