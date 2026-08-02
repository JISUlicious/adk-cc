"""Pre-invocation compaction: summarize an oversized inherited history
BEFORE the turn's first model call.

The cross-turn layer of the context-defense plan. ADK's own compaction is
post-invocation AND trigger-counted via the last event's model-reported
usage — which goes stale exactly when a session gets poisoned: after the
2026-08-02 overflow, the failed session's last usage said 76,349 (< the
140k threshold) while every future turn would replay ~289k tokens, so
post-turn compaction would NEVER fire and every turn would die the same
way. This plugin measures the events payload-inclusively (no usage
shortcut) at `before_run` — the session is quiescent, the one safe moment
to append a compaction event — and invokes the SAME summarizer stack ADK
compaction uses (`_make_compaction_summarizer`: model resolution, timeout,
breaker, churn floor, audit events all ride along).

Positionally identical to ADK's own append: ADK adds its compaction event
at the end of the list after an invocation; this adds it at the end of the
list before one. The request builder replaces the covered range with the
summary either way.

Fires at most once per invocation; failure of any kind proceeds without
compaction (the microcompact/guard layers still protect the call). Enabled
whenever compaction is configured (needs ADK_CC_COMPACTION_TOKEN_THRESHOLD);
kill-switch ADK_CC_PRECOMPACT=0.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin

from ..config.schema import env_bool
from ..permissions.token_counter import estimate_events_tokens

_log = logging.getLogger(__name__)

# How many trailing events stay out of the summarized range — mirrors the
# retention idea of ADK's compaction config (recent events are what the
# model is actively working from).
_KEEP_TAIL = 6


def _enabled() -> bool:
    return env_bool("ADK_CC_PRECOMPACT", default=True)


def _threshold() -> Optional[int]:
    raw = os.environ.get("ADK_CC_COMPACTION_TOKEN_THRESHOLD")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


class PrecompactPlugin(BasePlugin):
    """Measured pre-turn compaction for oversized inherited history."""

    def __init__(self, name: str = "adk_cc_precompact") -> None:
        super().__init__(name=name)

    async def before_run_callback(self, *, invocation_context: Any):
        if not _enabled():
            return None
        threshold = _threshold()
        if not threshold:
            return None
        try:
            session = invocation_context.session
            events = list(getattr(session, "events", None) or [])
            if len(events) <= _KEEP_TAIL:
                return None
            measured = estimate_events_tokens(events)
            if measured < threshold:
                return None

            head = events[:-_KEEP_TAIL]
            _log.info(
                "precompact: session %s measures ~%d tokens (threshold %d) — "
                "summarizing %d event(s) before the turn",
                getattr(session, "id", "?"), measured, threshold, len(head),
            )
            # Lazy import: agent.py imports this module at load time.
            from ..agent import _make_compaction_summarizer

            summarizer = _make_compaction_summarizer()
            compaction_event = await summarizer.maybe_summarize_events(events=head)
            if compaction_event is None:
                # Summarizer declined (churn floor, breaker, failure) — the
                # per-call layers still protect this turn.
                return None
            svc = getattr(invocation_context, "session_service", None)
            if svc is None:
                return None
            await svc.append_event(session, compaction_event)
            after = estimate_events_tokens(list(getattr(session, "events", None) or []))
            _log.info("precompact: appended compaction event — session now "
                      "measures ~%d tokens", after)
            try:
                from .audit import emit_audit_event
                emit_audit_event({
                    "event": "context_precompact",
                    "session_id": getattr(session, "id", "?"),
                    "before_tokens": measured, "after_tokens": after,
                    "compacted_events": len(head),
                })
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001 — never block a turn on prevention
            _log.warning("precompact skipped (%s: %s)", type(e).__name__, e)
        return None
