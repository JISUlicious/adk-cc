"""Env-driven logging configuration for the `adk_cc` logger tree.

Why this exists
---------------

Today every `adk_cc` module uses `logging.getLogger(__name__)`, which
gives a logger named `adk_cc.<submodule>`. None of those loggers have
handlers attached; their output propagates to the root logger, where
ADK's default leaves it printing to stderr with Python's default
formatter. There's no operator-facing knob for global verbosity, no
DEBUG-level traffic, no JSON-output option for log-aggregator pipes.

`configure_logging()` attaches a single handler to the `adk_cc` logger
(the parent of every submodule logger) and sets its level from
`ADK_CC_LOG_LEVEL`. The format is text by default, JSON when
`ADK_CC_LOG_FORMAT=json` is set.

Environment variables
---------------------

- `ADK_CC_LOG_LEVEL` (default `INFO`)
    `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` — standard
    `logging` level names. Case-insensitive.

- `ADK_CC_LOG_FORMAT` (default `text`)
    `text` — `LEVEL adk_cc.module: message` (human-scan friendly).
    `json` — one JSON object per record, fields: `ts`, `level`,
        `logger`, `message`, plus any `extra=` kwargs passed at the
        callsite.

Idempotency
-----------

Safe to call multiple times (a test, an `agent.py` reimport, a
runtime reconfigure). We tag our handler with a sentinel attribute
and refuse to add a second one. Level is always reapplied (so flipping
the env var + calling again actually changes the level).

Propagation stays at its default (True). Operators who installed
their own root-logger handler still see `adk_cc.*` records there;
this just guarantees adk-cc has its OWN handler even when the root
is unconfigured.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Optional

# Sentinel attribute on our handler so `configure_logging()` is
# idempotent. If we see a handler with this attribute already attached
# to the `adk_cc` logger, we don't add another.
_HANDLER_MARKER = "_adk_cc_managed"

# The logger we configure. All submodule loggers (`adk_cc.plugins.*`,
# `adk_cc.tools.*`, etc.) cascade up to this one via Python's logging
# hierarchy.
_ROOT_LOGGER_NAME = "adk_cc"


def configure_logging() -> None:
    """Read env vars and apply logging configuration to the `adk_cc`
    logger. Idempotent and safe to call from `agent.py` at import."""
    level = _level_from_env()
    fmt = _format_from_env()

    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(level)

    if _has_managed_handler(logger):
        # Already wired; just re-apply the level above and return.
        # The format isn't re-applied on a second call because changing
        # it mid-run would silently affect other tests. Restart the
        # process to switch format.
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(_make_formatter(fmt))
    setattr(handler, _HANDLER_MARKER, True)
    logger.addHandler(handler)


def _level_from_env() -> int:
    raw = os.environ.get("ADK_CC_LOG_LEVEL", "INFO").strip().upper()
    # `logging.getLevelName` is the inverse mapping (name → int) for
    # known names; for unknowns it returns the string "Level <name>",
    # which we treat as a typo and fall back to INFO.
    value = logging.getLevelName(raw)
    if isinstance(value, int):
        return value
    return logging.INFO


def _format_from_env() -> str:
    raw = os.environ.get("ADK_CC_LOG_FORMAT", "text").strip().lower()
    return "json" if raw == "json" else "text"


def _make_formatter(fmt: str) -> logging.Formatter:
    if fmt == "json":
        return _JsonFormatter()
    # Text format — `level logger: message`. Keep it short; operators
    # scanning stderr in a terminal want signal, not boilerplate.
    return logging.Formatter("%(levelname)s %(name)s: %(message)s")


def _has_managed_handler(logger: logging.Logger) -> bool:
    return any(getattr(h, _HANDLER_MARKER, False) for h in logger.handlers)


class _JsonFormatter(logging.Formatter):
    """One JSON object per record. Keeps fields stable so a downstream
    log shipper (Loki, Datadog, CloudWatch) can index them.

    Extras passed via `logger.debug("...", extra={"k": "v"})` end up
    as top-level keys alongside the standard ones. We filter out
    `logging`'s internal attributes so the JSON isn't polluted with
    `args`, `msg`, `pathname`, `process`, etc."""

    _RESERVED = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process",
        "taskName",  # Py 3.12+
    })

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Pull non-reserved fields from the record's __dict__ — these
        # are the `extra=` kwargs the callsite passed.
        for k, v in record.__dict__.items():
            if k in self._RESERVED or k.startswith("_"):
                continue
            if k in out:
                continue  # don't let extra override our standard keys
            out[k] = v
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def get_log_level() -> int:
    """Lightweight accessor for tests / callsites that want to check
    whether DEBUG is enabled before doing expensive log-prep work."""
    return logging.getLogger(_ROOT_LOGGER_NAME).getEffectiveLevel()
