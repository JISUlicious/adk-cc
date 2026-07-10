"""In-memory auth rate limiting.

Two primitives the public auth endpoints compose:

  - `SlidingWindowLimiter` — a per-key request budget (per-IP burst guard).
  - `FailureLockout` — N failed logins within the window locks the
    (ip, email) pair until the oldest failure ages out. Keying on the PAIR
    means an attacker elsewhere can't lock a victim out of their own account,
    while one machine still can't hammer one account.

Per-process and in-memory (resets on restart) — right-sized for the
self-hosted single-instance deployment; swap for a Redis-backed impl behind
the same two classes at scale.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

_PRUNE_AT = 4096  # amortized cleanup threshold (keys)


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        """Record one hit; False when the key is over budget for the window."""
        now = time.time()
        with self._lock:
            self._maybe_prune(now)
            q = self._hits[key]
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def _maybe_prune(self, now: float) -> None:
        if len(self._hits) < _PRUNE_AT:
            return
        cutoff = now - self.window_s
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
            del self._hits[k]


class FailureLockout:
    """`threshold` failures within `lockout_s` → locked until the oldest
    failure ages out. A success clears the key."""

    def __init__(self, threshold: int, lockout_s: float) -> None:
        self.threshold = threshold
        self.lockout_s = lockout_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def locked_for(self, key: str) -> float:
        """Seconds until the key unlocks; 0 when not locked."""
        now = time.time()
        with self._lock:
            q = self._fails.get(key)
            if not q:
                return 0.0
            while q and q[0] <= now - self.lockout_s:
                q.popleft()
            if not q:
                del self._fails[key]
                return 0.0
            if len(q) >= self.threshold:
                return q[0] + self.lockout_s - now
            return 0.0

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            if len(self._fails) >= _PRUNE_AT:
                cutoff = now - self.lockout_s
                for k in [k for k, q in self._fails.items() if not q or q[-1] <= cutoff]:
                    del self._fails[k]
            self._fails[key].append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
