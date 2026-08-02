"""In-flight model retry status, keyed by session.

Measured live (2026-08-02): a session on a rate-limited endpoint sat
"running" for 4+ minutes of 120s backoff sleeps with ZERO user-visible
feedback — indistinguishable from a hang — until the retry ladder finally
failed. The model layer knows when it is waiting; this module is how that
knowledge reaches the turn broker (and from there the UI).

Flow: `ModelSessionPlugin` stamps the current session key into a contextvar
before every model call (the same pattern as the model-pin override, and with
the same property — a reused task can never leak a previous session's key).
`SelectableLlm` publishes under that key before each backoff sleep and clears
on recovery/exit. `Turn.snapshot()` reads it, so the status rides the
`/api/turns/*` responses the UI already understands.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any, Optional

_session_key: ContextVar[Optional[str]] = ContextVar(
    "adk_cc_retry_session_key", default=None)

# session key -> status dict. Process-global like the subagents registry.
_status: dict[str, dict[str, Any]] = {}

# Self-heal horizon: a publisher that died mid-sleep (task killed, process
# fork) never cleared its entry. Anything this long past its resume time is
# garbage, not news.
_STALE_AFTER_S = 600.0


def set_current_session(key: Optional[str]) -> None:
    """Bind (or clear) the session key for model calls on this task."""
    _session_key.set(key)


def current_session() -> Optional[str]:
    return _session_key.get()


def publish_retry(*, model: str, attempt: int, of: int, delay_s: float,
                  kind: str) -> None:
    """Record that the current session's model call is sleeping out a
    rate limit. No-op when no session key is bound (out-of-band callers:
    session titles, memory scheduler, warmup)."""
    key = _session_key.get()
    if not key:
        return
    _status[key] = {
        "state": "rate_limited",
        "model": model,
        "attempt": attempt,
        "of": of,
        "reason": kind,
        "resume_at": time.time() + delay_s,
    }


def clear_retry() -> None:
    key = _session_key.get()
    if key:
        _status.pop(key, None)


def get_status(key: str) -> Optional[dict[str, Any]]:
    """Status for a session key, with a countdown the client can show
    directly. Stale entries are dropped, not served."""
    st = _status.get(key)
    if not st:
        return None
    remaining = st["resume_at"] - time.time()
    if remaining < -_STALE_AFTER_S:
        _status.pop(key, None)
        return None
    out = dict(st)
    out["resume_in_s"] = max(0.0, round(remaining, 1))
    return out
