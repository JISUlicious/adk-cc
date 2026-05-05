"""Unit tests for the session-retry wrapper.

Covers:
  - Disabled when env var is unset (no patching).
  - Patches SqliteSessionService when env var is set.
  - Idempotent: re-installing doesn't double-wrap.
  - Retries once on stale-session ValueError, succeeds on second try.
  - Surfaces non-stale ValueErrors immediately (no retry).
  - Refresh failure surfaces the original stale error.
  - Two consecutive stale errors raise (single-retry semantics).

Run: `uv run python tests/test_session_retry.py`
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")


def _reset_modules():
    for m in list(sys.modules):
        if m.startswith("adk_cc"):
            del sys.modules[m]


def _make_fake_session_service(*, append_behavior, refresh_returns):
    """Build a fake session service class and instance.

    `append_behavior`: list of "ok" / "stale" / ValueError. Pop one per call.
    `refresh_returns`: a session-like object returned by get_session.
    """

    class _FakeService:
        def __init__(self):
            self.append_calls = 0
            self.refresh_calls = 0
            self._behavior = list(append_behavior)

        async def append_event(self, session, event):
            self.append_calls += 1
            outcome = self._behavior.pop(0)
            if outcome == "ok":
                return f"ok-{self.append_calls}"
            if outcome == "stale":
                raise ValueError(
                    "The last_update_time provided in the session object is "
                    "earlier than the update_time in storage. Please check "
                    "if it is a stale session."
                )
            raise outcome  # actual exception instance

        async def get_session(self, *, app_name, user_id, session_id):
            self.refresh_calls += 1
            return refresh_returns

    return _FakeService


class _FakeSession:
    def __init__(self):
        self.app_name = "adk_cc"
        self.user_id = "alice"
        self.id = "sess-1"
        self.last_update_time = 1.0
        self.event_sequence = 0


# === Tests ===


def test_disabled_when_env_unset():
    print("test_disabled_when_env_unset: ", end="")
    os.environ.pop("ADK_CC_SESSION_RETRY_ON_STALE", None)
    _reset_modules()

    # Importing plugins triggers session_retry.install_retry_on_stale().
    # With the env unset, it should be a no-op — the real ADK class
    # should NOT be patched.
    from adk_cc.plugins import session_retry  # noqa: F401
    from google.adk.sessions.sqlite_session_service import SqliteSessionService

    assert not getattr(SqliteSessionService, "_adk_cc_retry_on_stale_patched", False)
    print("OK")


def test_patches_when_env_set():
    print("test_patches_when_env_set: ", end="")
    os.environ["ADK_CC_SESSION_RETRY_ON_STALE"] = "1"
    _reset_modules()

    from adk_cc.plugins import session_retry  # noqa: F401
    from google.adk.sessions.sqlite_session_service import SqliteSessionService

    assert getattr(SqliteSessionService, "_adk_cc_retry_on_stale_patched", False)
    print("OK")


def test_idempotent_install():
    print("test_idempotent_install: ", end="")
    os.environ["ADK_CC_SESSION_RETRY_ON_STALE"] = "1"
    _reset_modules()

    from adk_cc.plugins.session_retry import install_retry_on_stale
    from google.adk.sessions.sqlite_session_service import SqliteSessionService

    fn1 = SqliteSessionService.append_event
    install_retry_on_stale()  # second call — should be no-op
    install_retry_on_stale()  # third call — also no-op
    fn2 = SqliteSessionService.append_event
    assert fn1 is fn2, "append_event was re-wrapped on subsequent install_retry_on_stale calls"
    print("OK")


def test_retry_after_stale_succeeds():
    print("test_retry_after_stale_succeeds: ", end="")
    os.environ["ADK_CC_SESSION_RETRY_ON_STALE"] = "1"
    _reset_modules()

    from adk_cc.plugins.session_retry import _patch

    fresh_session = _FakeSession()
    fresh_session.last_update_time = 99.0
    fresh_session.event_sequence = 7

    cls = _make_fake_session_service(
        append_behavior=["stale", "ok"],
        refresh_returns=fresh_session,
    )
    _patch(cls)
    svc = cls()
    session = _FakeSession()

    result = asyncio.run(svc.append_event(session, event="evt"))
    assert result == "ok-2", f"expected ok-2, got {result}"
    assert svc.append_calls == 2
    assert svc.refresh_calls == 1
    # Session ref synced from refresh response.
    assert session.last_update_time == 99.0
    assert session.event_sequence == 7
    print("OK")


def test_non_stale_value_error_passes_through():
    print("test_non_stale_value_error_passes_through: ", end="")
    os.environ["ADK_CC_SESSION_RETRY_ON_STALE"] = "1"
    _reset_modules()
    from adk_cc.plugins.session_retry import _patch

    cls = _make_fake_session_service(
        append_behavior=[ValueError("session not found")],
        refresh_returns=None,
    )
    _patch(cls)
    svc = cls()

    try:
        asyncio.run(svc.append_event(_FakeSession(), event="evt"))
        print("FAIL: expected ValueError")
        sys.exit(1)
    except ValueError as e:
        assert "session not found" in str(e)
        # No retry: refresh wasn't even called.
        assert svc.refresh_calls == 0
        print("OK")


def test_refresh_failure_raises_original_stale():
    print("test_refresh_failure_raises_original_stale: ", end="")
    os.environ["ADK_CC_SESSION_RETRY_ON_STALE"] = "1"
    _reset_modules()
    from adk_cc.plugins.session_retry import _patch

    class _BrokenRefresh:
        def __init__(self):
            self.append_calls = 0

        async def append_event(self, session, event):
            self.append_calls += 1
            raise ValueError("stale session: please reload")

        async def get_session(self, *, app_name, user_id, session_id):
            raise ConnectionError("storage unreachable")

    _patch(_BrokenRefresh)
    svc = _BrokenRefresh()
    try:
        asyncio.run(svc.append_event(_FakeSession(), event="evt"))
        print("FAIL: expected ValueError")
        sys.exit(1)
    except ValueError as e:
        # The wrapper raises the original stale ValueError, NOT the
        # ConnectionError, so the runner sees a coherent failure shape.
        assert "stale" in str(e).lower()
        assert svc.append_calls == 1, "should not have retried append after refresh failed"
        print("OK")


def test_double_stale_raises():
    """Single-retry semantics: if the second append also returns stale, raise."""
    print("test_double_stale_raises: ", end="")
    os.environ["ADK_CC_SESSION_RETRY_ON_STALE"] = "1"
    _reset_modules()
    from adk_cc.plugins.session_retry import _patch

    cls = _make_fake_session_service(
        append_behavior=["stale", "stale"],
        refresh_returns=_FakeSession(),
    )
    _patch(cls)
    svc = cls()

    try:
        asyncio.run(svc.append_event(_FakeSession(), event="evt"))
        print("FAIL: expected ValueError")
        sys.exit(1)
    except ValueError as e:
        assert "stale" in str(e).lower()
        assert svc.append_calls == 2, "expected exactly two append attempts"
        print("OK")


def main():
    test_disabled_when_env_unset()
    test_patches_when_env_set()
    test_idempotent_install()
    test_retry_after_stale_succeeds()
    test_non_stale_value_error_passes_through()
    test_refresh_failure_raises_original_stale()
    test_double_stale_raises()
    print()
    print("All session-retry tests passed")


if __name__ == "__main__":
    main()
