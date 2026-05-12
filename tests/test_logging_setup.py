"""Unit tests for `adk_cc/logging_setup.py`.

Covers:
  - Default level (INFO) when no env var is set.
  - `ADK_CC_LOG_LEVEL` honored (DEBUG, WARNING, etc).
  - Invalid level falls back to INFO silently.
  - Text format (default) vs JSON format (env opt-in).
  - JSON format includes `extra=` kwargs as top-level keys.
  - Idempotent — calling twice doesn't double-add handlers.
  - The configured handler attaches to the `adk_cc` parent logger so
    submodule loggers (e.g. `adk_cc.plugins.permissions`) cascade
    through it.

Run: `.venv/bin/python tests/test_logging_setup.py`
"""

from __future__ import annotations

import io
import json
import logging
import os

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.logging_setup import (
    _HANDLER_MARKER,
    _ROOT_LOGGER_NAME,
    configure_logging,
    get_log_level,
)


def _reset_adk_cc_logger() -> None:
    """Drop any handlers our previous test added so each test starts
    clean. We only touch handlers tagged with our sentinel so any
    operator-installed handlers (none in tests, but defensive) are
    preserved."""
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    keep = [h for h in logger.handlers if not getattr(h, _HANDLER_MARKER, False)]
    logger.handlers = keep
    # Reset level so the level-from-env test isn't poisoned by a
    # previous test that set it.
    logger.setLevel(logging.NOTSET)


# --- Level ---------------------------------------------------------


def test_default_level_is_info() -> None:
    _reset_adk_cc_logger()
    os.environ.pop("ADK_CC_LOG_LEVEL", None)
    configure_logging()
    assert get_log_level() == logging.INFO
    print("OK test_default_level_is_info")


def test_env_sets_debug_level() -> None:
    _reset_adk_cc_logger()
    os.environ["ADK_CC_LOG_LEVEL"] = "DEBUG"
    try:
        configure_logging()
        assert get_log_level() == logging.DEBUG
    finally:
        os.environ.pop("ADK_CC_LOG_LEVEL", None)
    print("OK test_env_sets_debug_level")


def test_env_case_insensitive() -> None:
    _reset_adk_cc_logger()
    os.environ["ADK_CC_LOG_LEVEL"] = "warning"
    try:
        configure_logging()
        assert get_log_level() == logging.WARNING
    finally:
        os.environ.pop("ADK_CC_LOG_LEVEL", None)
    print("OK test_env_case_insensitive")


def test_invalid_level_falls_back_to_info() -> None:
    """A typo'd value (`INFOO`) silently falls back rather than
    crashing — agent boot mustn't die on a log-config misspelling."""
    _reset_adk_cc_logger()
    os.environ["ADK_CC_LOG_LEVEL"] = "INFOO"
    try:
        configure_logging()
        assert get_log_level() == logging.INFO
    finally:
        os.environ.pop("ADK_CC_LOG_LEVEL", None)
    print("OK test_invalid_level_falls_back_to_info")


# --- Idempotency ---------------------------------------------------


def test_configure_logging_is_idempotent() -> None:
    """Multiple calls don't double-add handlers — important because
    agent.py calls it at import, and a re-import (or test fixture)
    might trigger a second call."""
    _reset_adk_cc_logger()
    configure_logging()
    configure_logging()
    configure_logging()
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    managed_handlers = [h for h in logger.handlers if getattr(h, _HANDLER_MARKER, False)]
    assert len(managed_handlers) == 1, managed_handlers
    print("OK test_configure_logging_is_idempotent")


def test_handler_attaches_to_adk_cc_logger() -> None:
    """The handler goes on `adk_cc`, not the root. So submodule
    loggers like `adk_cc.plugins.permissions` cascade through it
    without us touching every individual logger."""
    _reset_adk_cc_logger()
    configure_logging()
    adk_cc_logger = logging.getLogger(_ROOT_LOGGER_NAME)
    root_logger = logging.getLogger()
    assert any(getattr(h, _HANDLER_MARKER, False) for h in adk_cc_logger.handlers)
    assert not any(getattr(h, _HANDLER_MARKER, False) for h in root_logger.handlers)
    print("OK test_handler_attaches_to_adk_cc_logger")


# --- Format --------------------------------------------------------


def _capture_format(env_format: str | None) -> str:
    """Reconfigure logging with the given format env var, log a
    sample line, and return what hit the stream."""
    _reset_adk_cc_logger()
    if env_format is None:
        os.environ.pop("ADK_CC_LOG_FORMAT", None)
    else:
        os.environ["ADK_CC_LOG_FORMAT"] = env_format
    try:
        configure_logging()
        # Swap the managed handler's stream for capture.
        adk_cc_logger = logging.getLogger(_ROOT_LOGGER_NAME)
        managed = next(
            h for h in adk_cc_logger.handlers if getattr(h, _HANDLER_MARKER, False)
        )
        buf = io.StringIO()
        managed.stream = buf
        adk_cc_logger.setLevel(logging.DEBUG)
        managed.setLevel(logging.DEBUG)
        logging.getLogger("adk_cc.test").debug(
            "hello world", extra={"tool_name": "run_bash", "behavior": "ask"}
        )
        return buf.getvalue()
    finally:
        os.environ.pop("ADK_CC_LOG_FORMAT", None)


def test_text_format_by_default() -> None:
    """No env var → plain `LEVEL logger: message` format."""
    out = _capture_format(None)
    assert "DEBUG adk_cc.test:" in out, out
    assert "hello world" in out, out
    # No JSON braces.
    assert not out.startswith("{"), out
    print("OK test_text_format_by_default")


def test_json_format_opt_in() -> None:
    """`ADK_CC_LOG_FORMAT=json` → one JSON object per line with
    the documented field shape, including `extra=` kwargs."""
    out = _capture_format("json").strip()
    parsed = json.loads(out)
    assert parsed["level"] == "DEBUG"
    assert parsed["logger"] == "adk_cc.test"
    assert parsed["message"] == "hello world"
    assert isinstance(parsed["ts"], (int, float))
    # `extra=` kwargs surface as top-level keys.
    assert parsed["tool_name"] == "run_bash"
    assert parsed["behavior"] == "ask"
    print("OK test_json_format_opt_in")


def test_json_format_strips_internal_logging_fields() -> None:
    """`logging.LogRecord` has many internal attributes (pathname,
    process, etc) — the JSON formatter filters them so output stays
    clean."""
    out = _capture_format("json").strip()
    parsed = json.loads(out)
    for noisy in ("pathname", "process", "thread", "msecs", "filename"):
        assert noisy not in parsed, (noisy, parsed)
    print("OK test_json_format_strips_internal_logging_fields")


# --- Driver --------------------------------------------------------


def main() -> None:
    test_default_level_is_info()
    test_env_sets_debug_level()
    test_env_case_insensitive()
    test_invalid_level_falls_back_to_info()
    test_configure_logging_is_idempotent()
    test_handler_attaches_to_adk_cc_logger()
    test_text_format_by_default()
    test_json_format_opt_in()
    test_json_format_strips_internal_logging_fields()
    print("\nall logging-setup tests passed")


if __name__ == "__main__":
    main()
