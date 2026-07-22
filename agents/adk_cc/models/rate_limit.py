"""429 classification — one retry ladder cannot serve three throttles.

OpenRouter (openrouter.ai/docs/api-reference/limits) — and providers generally
— emit three distinct kinds of rate limit, each deserving a different response:

  - ``burst``    per-minute caps (OpenRouter: 20 req/min on ``:free``).
                 Short window → the standard ladder (base·2^n) fits.
  - ``upstream`` the model's own upstream is throttled (e.g. Google AI Studio
                 behind an OpenRouter ``:free`` route; body says "temporarily
                 rate-limited upstream"). Minutes-scale, opaque → a slower
                 ladder is worth it before giving up.
  - ``quota``    a daily/monthly cap (OpenRouter free tier: 50/day, resets
                 UTC midnight; ``X-RateLimit-Reset`` is hours away). Retrying
                 inside a turn is POINTLESS — fail fast with a message that
                 names the reset time so the user can switch models instead.

Classification is best-effort from the exception's response headers and body
text; unknown shapes default to ``burst`` (today's behavior). Stdlib-only.
"""

from __future__ import annotations

import re
import time
from typing import Optional

# A reset further away than this is a quota wall, not a burst window.
QUOTA_HORIZON_S = 15 * 60

_UPSTREAM_PAT = re.compile(
    r"rate-?limited upstream|provider returned error|upstream.*rate.?limit",
    re.IGNORECASE,
)


def _headers(err: BaseException):
    resp = getattr(err, "response", None)
    h = getattr(resp, "headers", None)
    return h if (h is not None and hasattr(h, "get")) else None


def _float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def classify_429(err: BaseException) -> tuple[str, Optional[float]]:
    """Return ``(kind, reset_hint_s)`` for a rate-limit error.

    ``kind`` ∈ ``burst`` | ``upstream`` | ``quota``. ``reset_hint_s`` is the
    seconds-until-reset when the provider told us (Retry-After, or an
    ``X-RateLimit-Reset`` epoch/delta), else None.
    """
    hint: Optional[float] = None
    h = _headers(err)
    if h is not None:
        hint = _float(h.get("retry-after"))
        if hint is None:
            reset = _float(h.get("x-ratelimit-reset"))
            if reset is not None:
                # Header may be an epoch (s or ms) or a plain delta.
                if reset > 1e12:  # epoch ms
                    hint = max(0.0, reset / 1000.0 - time.time())
                elif reset > 1e9:  # epoch s
                    hint = max(0.0, reset - time.time())
                else:
                    hint = reset
    if hint is not None and hint > QUOTA_HORIZON_S:
        return "quota", hint
    body = str(err)
    if _UPSTREAM_PAT.search(body):
        return "upstream", hint
    if "quota" in body.lower() or "per day" in body.lower():
        return "quota", hint
    return "burst", hint


def describe_quota(reset_hint_s: Optional[float]) -> str:
    """Human line for the fail-fast quota message."""
    if reset_hint_s is None:
        return (
            "provider quota exhausted — free-tier daily caps reset at "
            "00:00 UTC; switch the session to another model (/model) or retry later."
        )
    hrs, rem = divmod(int(reset_hint_s), 3600)
    mins = rem // 60
    return (
        f"provider quota exhausted — resets in ~{hrs}h{mins:02d}m; switch the "
        "session to another model (/model) or retry then."
    )
