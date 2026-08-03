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
# Uncached results the digest may summarize inline per precompact (each is
# cached forever, so this is a per-episode bound, not a recurring cost).
_MAX_COMPUTED_SUMMARIES = 12


def _enabled() -> bool:
    return env_bool("ADK_CC_PRECOMPACT", default=True)


def _guard_reject_line() -> Optional[int]:
    """The context guard's REJECT watermark — force-mode trigger. None when
    the guard is disabled (no reject, no loop to exit)."""
    try:
        from .context_guard import resolved_limits

        limits = resolved_limits()
        return int(limits["reject"]) if limits else None
    except Exception:  # noqa: BLE001
        return None


async def _digest_head_for_summary(head: list[Any]) -> tuple[list[Any], int]:
    """Deep-copied head events with TOOL material swapped for its cached
    summaries (heads when uncached): function_response payloads and ADK's
    foreign-tool-result text renderings. Genuine prose is left intact —
    condensing conversation is exactly the summarizer's job.

    P1.5: the whole-window summarizer previously re-read raw bulk the
    per-call rewriter had already paid to summarize — and on payload-heavy
    heads could exceed its own window. Digested input is always small.
    Originals are untouched (the compaction range still reads the real
    events; only the summarizer's INPUT is condensed)."""
    import json as _json

    from ..context import result_summaries
    from .microcompact import _FOREIGN_RESULT_RE

    out: list[Any] = []
    saved = 0
    computed = 0
    for ev in head:
        try:
            copy = ev.model_copy(deep=True)
        except Exception:  # noqa: BLE001 — undigested beats broken
            out.append(ev)
            continue
        for part in (getattr(getattr(copy, "content", None), "parts", None) or []):
            fr = getattr(part, "function_response", None)
            if fr is not None:
                try:
                    raw = _json.dumps(getattr(fr, "response", None) or {},
                                      default=str)
                except Exception:  # noqa: BLE001
                    continue
                if len(raw) < 2048:
                    continue
                summary = result_summaries.cached(raw)
                if summary is None and computed < _MAX_COMPUTED_SUMMARIES:
                    # Compute the missing summary NOW (cached forever).
                    # Measured live: head-only digests dropped the very facts
                    # the next question needed — the model then answered
                    # confidently wrong from partial material.
                    summary = await result_summaries.summarize(
                        raw, tool=getattr(fr, "name", None) or "tool")
                    if summary is not None:
                        computed += 1
                fr.response = ({"summary": summary} if summary
                               else {"head": raw[:300]})
                saved += len(raw)
                continue
            text = getattr(part, "text", None)
            if text and len(text) >= 2048 and _FOREIGN_RESULT_RE.match(text):
                summary = result_summaries.cached(text)
                if summary is None and computed < _MAX_COMPUTED_SUMMARIES:
                    summary = await result_summaries.summarize(text, tool="tool")
                    if summary is not None:
                        computed += 1
                part.text = summary if summary else text[:300]
                saved += len(text) - len(part.text)
        out.append(copy)
    return out, saved


def _mechanical_compaction_event(head: list[Any]) -> Any:
    """A compaction event built WITHOUT a model — the guaranteed exit when
    the summarizer is dead or the material exceeds its own window. Content
    is a bounded per-event digest: text heads verbatim, tool results as
    their cached summaries when the rewriter already paid for one, heads
    otherwise. Lossier than an LLM summary, better than a dead session."""
    import json as _json

    from google.adk.events.event import Event
    from google.adk.events.event_actions import EventActions, EventCompaction
    from google.genai import types

    from ..context import result_summaries

    lines = ["[Mechanically condensed history — the summarizer was "
             "unavailable; details are lossy. Re-run tools if specifics "
             "are needed.]"]
    budget = 12_000
    for ev in head:
        if sum(len(x) for x in lines) > budget:
            lines.append("[…further events truncated]")
            break
        author = getattr(ev, "author", "?")
        for part in (getattr(getattr(ev, "content", None), "parts", None) or []):
            text = getattr(part, "text", None)
            if text and not getattr(part, "thought", False):
                lines.append(f"[{author}] {text[:300]}")
                continue
            fc = getattr(part, "function_call", None)
            if fc is not None:
                lines.append(f"[{author}] called {getattr(fc, 'name', '?')}")
                continue
            fr = getattr(part, "function_response", None)
            if fr is not None:
                try:
                    raw = _json.dumps(getattr(fr, "response", None) or {},
                                      default=str)
                except Exception:  # noqa: BLE001
                    raw = str(getattr(fr, "response", None))
                summary = result_summaries.cached(raw)
                body = summary if summary else raw[:200]
                lines.append(f"[{author}] {getattr(fr, 'name', '?')} -> {body}")

    return Event(
        author="user",
        invocation_id=Event.new_id(),
        actions=EventActions(compaction=EventCompaction(
            start_timestamp=getattr(head[0], "timestamp", 0.0),
            end_timestamp=getattr(head[-1], "timestamp", 0.0),
            compacted_content=types.Content(
                role="model", parts=[types.Part(text="\n".join(lines))]),
        )),
    )


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
        force_line = _guard_reject_line()
        if not threshold and not force_line:
            return None
        try:
            session = invocation_context.session
            events = list(getattr(session, "events", None) or [])
            if len(events) <= 3:
                return None
            measured = estimate_events_tokens(events)
            # FORCE mode — the reject-loop exit (reported: a guard REJECT
            # reran the identical pipeline forever). At the guard's reject
            # watermark this plugin compacts regardless of whether normal
            # compaction is configured, keeps a minimal tail, and if the
            # summarizer cannot help, falls back to a MECHANICAL (model-free)
            # digest — the session ALWAYS durably shrinks, so the next call
            # gets a smaller request instead of the same rejection.
            force = bool(force_line) and measured >= force_line
            if not force and (not threshold or measured < threshold):
                return None

            keep_tail = 2 if force else _KEEP_TAIL
            if len(events) <= keep_tail:
                return None
            head = events[:-keep_tail]
            _log.info(
                "precompact%s: session %s measures ~%d tokens (threshold %s, "
                "force_line %s) — summarizing %d event(s) before the turn",
                " FORCE" if force else "", getattr(session, "id", "?"),
                measured, threshold, force_line, len(head),
            )
            # Lazy import: agent.py imports this module at load time.
            from ..agent import _make_compaction_summarizer

            summarizer = _make_compaction_summarizer()
            digested, digest_saved = await _digest_head_for_summary(head)
            if digest_saved:
                _log.info(
                    "precompact: digested head for summarization (~%d chars "
                    "of tool material swapped for cached summaries/heads)",
                    digest_saved)
            try:
                # force=True in FORCE mode: the churn floor guards against
                # pointless re-compaction, not against a first compaction of
                # deliberately-small digested input (measured live: it pushed
                # a recoverable session onto the lossier mechanical path).
                compaction_event = await summarizer.maybe_summarize_events(
                    events=digested, force=force)
            except Exception as e:  # noqa: BLE001
                _log.warning("precompact: summarizer raised (%s: %s)",
                             type(e).__name__, str(e)[:150])
                compaction_event = None
            if compaction_event is None:
                if not force:
                    # Summarizer declined (churn floor, breaker, failure) —
                    # the per-call layers still protect this turn.
                    return None
                compaction_event = _mechanical_compaction_event(head)
                _log.warning(
                    "precompact FORCE: summarizer unavailable — appended a "
                    "MECHANICAL digest of %d event(s)", len(head))
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
