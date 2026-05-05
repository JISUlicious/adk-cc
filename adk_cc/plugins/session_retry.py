"""Retry stale-session writes once with a refreshed session reference.

ADK's `SqliteSessionService.append_event` (and `DatabaseSessionService`'s
equivalent) raises a `ValueError` with "stale session" / "modified in
storage" when storage's `update_time` is newer than the in-memory
session ref's `last_update_time`. In single-process `adk web` setups
this fires across HITL pause/resume cycles because the runner doesn't
refresh the session ref's timestamp after intermediate writes — it's
an upstream race we can't fix at the runner layer.

Mitigation at the session-service layer: catch the specific stale
ValueError, fetch the current session from storage to sync
`last_update_time` (and `event_sequence` once PR #4752 lands), retry
the append exactly once. If the second attempt also fails, raise —
that's a genuine concurrent-writer collision the application should
hear about.

Activated by `ADK_CC_SESSION_RETRY_ON_STALE=1`. Off by default so
operators have to opt in deliberately. Once on, the patch installs at
module import time (before ADK instantiates a session service via
`adk web` / `adk api_server`'s `local_storage.py`).

Caveats:
  - Single-retry semantics. Doesn't loop. If the session is genuinely
    being raced by N writers, only the first to commit wins; the rest
    surface as their original error after the retry.
  - If `get_session` itself fails during refresh, we raise the
    *original* stale error — the runner sees a coherent stale-session
    failure rather than a confusing fetch error.
  - Logs every retry at WARNING so operators can see if the patch is
    masking a real concurrency problem.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_log = logging.getLogger(__name__)

# Sentinel attribute on the patched class so re-imports don't double-wrap.
_PATCHED_FLAG = "_adk_cc_retry_on_stale_patched"

# Substrings that identify the specific ValueError shape we know how to
# recover from. Both SqliteSessionService and DatabaseSessionService raise
# ValueErrors with these markers for the same underlying optimistic-lock
# conflict.
_STALE_MARKERS = (
    "stale session",
    "modified in storage",
)


def _is_stale_session_error(exc: BaseException) -> bool:
    msg = str(exc)
    return any(marker in msg for marker in _STALE_MARKERS)


def _patch(cls: type) -> None:
    if getattr(cls, _PATCHED_FLAG, False):
        return

    original_append = cls.append_event

    async def append_event_with_retry(self, session: Any, event: Any) -> Any:
        try:
            return await original_append(self, session, event)
        except ValueError as e:
            if not _is_stale_session_error(e):
                raise

            _log.warning(
                "%s.append_event: stale session for session_id=%s — refreshing ref and retrying once.",
                cls.__name__,
                getattr(session, "id", "?"),
            )

            try:
                fresh = await self.get_session(
                    app_name=session.app_name,
                    user_id=session.user_id,
                    session_id=session.id,
                )
            except Exception as fetch_err:
                _log.error(
                    "%s.append_event: failed to refresh session ref (%s); surfacing original stale error.",
                    cls.__name__,
                    fetch_err,
                )
                raise e from fetch_err

            if fresh is None:
                # Session vanished between the failed append and the refresh.
                # Surface the stale error; nothing useful to retry with.
                raise e

            # Sync optimistic-lock state from storage onto the runner's
            # in-memory session ref. Both fields are checked by ADK's
            # session services depending on version (event_sequence is
            # the post-PR-#4752 mechanism).
            session.last_update_time = fresh.last_update_time
            if hasattr(fresh, "event_sequence"):
                session.event_sequence = fresh.event_sequence

            return await original_append(self, session, event)

    cls.append_event = append_event_with_retry
    setattr(cls, _PATCHED_FLAG, True)
    _log.info("Patched %s.append_event with retry-on-stale wrapper.", cls.__name__)


def install_retry_on_stale() -> None:
    """Install the retry wrapper on every available session service class.

    Triggered by `ADK_CC_SESSION_RETRY_ON_STALE=1`. Idempotent — repeated
    calls are no-ops once the flag is set. Defensive imports tolerate
    either class being unavailable in some ADK release.
    """
    if os.environ.get("ADK_CC_SESSION_RETRY_ON_STALE") != "1":
        return

    try:
        from google.adk.sessions.sqlite_session_service import SqliteSessionService

        _patch(SqliteSessionService)
    except ImportError:
        pass

    try:
        from google.adk.sessions.database_session_service import (
            DatabaseSessionService,
        )

        _patch(DatabaseSessionService)
    except ImportError:
        pass


# Side-effect on import so a single `from adk_cc.plugins import ...` (which
# fires through `plugins/__init__.py`) triggers the patch before ADK
# instantiates any session service.
install_retry_on_stale()
