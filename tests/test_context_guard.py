"""Unit tests for the context-length guardrail.

Covers `ContextGuardPlugin` (WARN log, REJECT short-circuit, disabled
no-op, token counter fallback) and the `EventsCompactionConfig` wiring
(env-driven construction, dedicated summarizer model).

Run: `uv run python tests/test_context_guard.py`

Should become a pytest module once pytest lands in dev deps.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from io import StringIO
from typing import Optional

# Force a dummy model API key so agent.py imports cleanly.
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")


# === Test infrastructure ===


def _reset_modules():
    """Drop adk_cc modules from sys.modules so re-imports pick up new env vars."""
    for m in list(sys.modules):
        if m.startswith("adk_cc"):
            del sys.modules[m]


def _clear_env(*keys):
    for k in keys:
        os.environ.pop(k, None)


def _capture_logs(logger_name: str, level: int = logging.WARNING) -> tuple[StringIO, logging.Handler]:
    """Returns (buf, handler) — caller must remove the handler afterwards."""
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(level)
    return buf, handler


def _varied_text(target_tokens: int, model: str = "openai/gpt-4") -> str:
    """Generate varied text close to a target token count under tiktoken.

    Repeated 'x' compresses to ~1 token per several thousand chars; varied
    UUID-style words count closer to 1 token each. This helper builds a
    string and verifies the actual count is within ~10% of the target.
    """
    import litellm
    import uuid

    words: list[str] = []
    while True:
        words.append(uuid.uuid4().hex[:8])
        if len(words) % 100 == 0:
            text = " ".join(words)
            actual = int(litellm.token_counter(model=model, text=text))
            if actual >= target_tokens:
                return text


def _build_request(*, target_tokens: int = 0, system_instruction: Optional[str] = None,
                   model: str = "openai/gpt-4"):
    """Build a fake LlmRequest with a controlled token count under tiktoken."""
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    text = _varied_text(target_tokens, model=model) if target_tokens > 0 else ""
    parts = [types.Part(text=text)] if text else []
    contents = [types.Content(role="user", parts=parts)] if parts else []

    config = types.GenerateContentConfig(system_instruction=system_instruction)
    return LlmRequest(model=model, contents=contents, config=config)


class _FakeSession:
    def __init__(self, sid: str = "test-session"): self.id = sid


class _FakeCallbackContext:
    def __init__(self, session_id: str = "test-session"):
        self._session = _FakeSession(session_id)

    @property
    def session(self): return self._session


# === Plugin behavior tests ===


def test_disabled_when_max_unset():
    print("test_disabled_when_max_unset: ", end="")
    _clear_env("ADK_CC_MAX_CONTEXT_TOKENS",
               "ADK_CC_CONTEXT_WARN_TOKENS", "ADK_CC_CONTEXT_REJECT_TOKENS")
    _reset_modules()
    from adk_cc.plugins.context_guard import ContextGuardPlugin

    plugin = ContextGuardPlugin()
    assert plugin._max is None
    # Build a small request — even with MAX unset, plugin should no-op fast.
    req = _build_request(target_tokens=100)
    result = asyncio.run(plugin.before_model_callback(
        callback_context=_FakeCallbackContext(), llm_request=req,
    ))
    assert result is None, "plugin should no-op when MAX unset"
    print("OK")


def test_no_action_below_warn():
    print("test_no_action_below_warn: ", end="")
    os.environ["ADK_CC_MAX_CONTEXT_TOKENS"] = "10000"
    _clear_env("ADK_CC_CONTEXT_WARN_TOKENS", "ADK_CC_CONTEXT_REJECT_TOKENS")
    _reset_modules()
    from adk_cc.plugins.context_guard import ContextGuardPlugin

    plugin = ContextGuardPlugin()
    # ~5000 tokens; under 7500 WARN.
    req = _build_request(target_tokens=5000)

    buf, handler = _capture_logs("adk_cc.plugins.context_guard")
    try:
        result = asyncio.run(plugin.before_model_callback(
            callback_context=_FakeCallbackContext(), llm_request=req,
        ))
    finally:
        logging.getLogger("adk_cc.plugins.context_guard").removeHandler(handler)

    assert result is None
    assert "WARN" not in buf.getvalue() and "REJECT" not in buf.getvalue()
    print("OK")


def test_warn_logs_no_mutation():
    print("test_warn_logs_no_mutation: ", end="")
    os.environ["ADK_CC_MAX_CONTEXT_TOKENS"] = "10000"
    _clear_env("ADK_CC_CONTEXT_WARN_TOKENS", "ADK_CC_CONTEXT_REJECT_TOKENS")
    _reset_modules()
    from adk_cc.plugins.context_guard import ContextGuardPlugin

    plugin = ContextGuardPlugin()
    # Above WARN (7500) but below REJECT (9500).
    req = _build_request(target_tokens=8000)
    contents_before = len(req.contents)

    buf, handler = _capture_logs("adk_cc.plugins.context_guard")
    try:
        result = asyncio.run(plugin.before_model_callback(
            callback_context=_FakeCallbackContext("sess-A"), llm_request=req,
        ))
    finally:
        logging.getLogger("adk_cc.plugins.context_guard").removeHandler(handler)

    assert result is None, "WARN should not return early"
    log_output = buf.getvalue()
    assert "WARN" in log_output, f"expected WARN log, got: {log_output!r}"
    assert "sess-A" in log_output, "expected session_id in log"
    assert len(req.contents) == contents_before, "contents should not be mutated"
    print("OK")


def test_reject_short_circuits():
    print("test_reject_short_circuits: ", end="")
    os.environ["ADK_CC_MAX_CONTEXT_TOKENS"] = "10000"
    _clear_env("ADK_CC_CONTEXT_WARN_TOKENS", "ADK_CC_CONTEXT_REJECT_TOKENS")
    _reset_modules()
    from adk_cc.plugins.context_guard import ContextGuardPlugin
    from google.adk.models.llm_response import LlmResponse

    plugin = ContextGuardPlugin()
    # ~10K tokens — over both WARN (7500) and REJECT (9500).
    req = _build_request(target_tokens=10000)

    result = asyncio.run(plugin.before_model_callback(
        callback_context=_FakeCallbackContext(), llm_request=req,
    ))
    assert isinstance(result, LlmResponse), f"expected LlmResponse, got {type(result)}"
    parts = result.content.parts if result.content else []
    text = "".join(p.text or "" for p in parts)
    assert "context" in text.lower() and "full" in text.lower(), \
        f"expected friendly stop text, got: {text!r}"
    print("OK")


def test_token_counter_fallback():
    """Force litellm.token_counter to fail; chars/4 fallback should still work."""
    print("test_token_counter_fallback: ", end="")
    os.environ["ADK_CC_MAX_CONTEXT_TOKENS"] = "1000"
    _clear_env("ADK_CC_CONTEXT_WARN_TOKENS", "ADK_CC_CONTEXT_REJECT_TOKENS")
    _reset_modules()
    from adk_cc.plugins.context_guard import ContextGuardPlugin
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    import litellm

    # Build a request with ~1000 token chars/4 estimate; force fallback.
    text = "x" * 4000  # 1000 tokens by chars/4
    req = LlmRequest(
        model="openai/gpt-4",
        contents=[types.Content(role="user", parts=[types.Part(text=text)])],
        config=types.GenerateContentConfig(),
    )
    original = litellm.token_counter
    litellm.token_counter = lambda **kw: (_ for _ in ()).throw(RuntimeError("forced failure"))
    try:
        plugin = ContextGuardPlugin()
        result = asyncio.run(plugin.before_model_callback(
            callback_context=_FakeCallbackContext(), llm_request=req,
        ))
        assert result is not None, "fallback path should still fire REJECT"
    finally:
        litellm.token_counter = original
    print("OK")


def test_absolute_overrides():
    print("test_absolute_overrides: ", end="")
    os.environ["ADK_CC_MAX_CONTEXT_TOKENS"] = "100000"
    os.environ["ADK_CC_CONTEXT_WARN_TOKENS"] = "1000"
    os.environ["ADK_CC_CONTEXT_REJECT_TOKENS"] = "2000"
    _reset_modules()
    from adk_cc.plugins.context_guard import ContextGuardPlugin

    plugin = ContextGuardPlugin()
    assert plugin._warn == 1000
    assert plugin._reject == 2000
    print("OK")


# === Compaction config wiring tests ===


def test_compaction_config_unset_returns_none():
    print("test_compaction_config_unset_returns_none: ", end="")
    _clear_env("ADK_CC_COMPACTION_TOKEN_THRESHOLD",
               "ADK_CC_COMPACTION_EVENT_RETENTION",
               "ADK_CC_COMPACTION_INTERVAL", "ADK_CC_COMPACTION_OVERLAP",
               "ADK_CC_COMPACTION_MODEL")
    _reset_modules()
    from adk_cc.agent import app
    assert app.events_compaction_config is None
    print("OK")


def test_compaction_config_token_threshold():
    print("test_compaction_config_token_threshold: ", end="")
    os.environ["ADK_CC_COMPACTION_TOKEN_THRESHOLD"] = "5000"
    os.environ["ADK_CC_COMPACTION_EVENT_RETENTION"] = "8"
    _clear_env("ADK_CC_COMPACTION_INTERVAL", "ADK_CC_COMPACTION_OVERLAP",
               "ADK_CC_COMPACTION_MODEL")
    _reset_modules()
    from adk_cc.agent import app
    cc = app.events_compaction_config
    assert cc is not None
    assert cc.token_threshold == 5000
    assert cc.event_retention_size == 8
    assert cc.summarizer is None  # ADK auto-defaults at first compaction
    print("OK")


def test_compaction_dedicated_model():
    print("test_compaction_dedicated_model: ", end="")
    os.environ["ADK_CC_COMPACTION_TOKEN_THRESHOLD"] = "5000"
    os.environ["ADK_CC_COMPACTION_EVENT_RETENTION"] = "8"
    os.environ["ADK_CC_COMPACTION_MODEL"] = "openai/cheap-stub"
    _reset_modules()
    from adk_cc.agent import app
    from google.adk.apps.llm_event_summarizer import LlmEventSummarizer

    cc = app.events_compaction_config
    assert cc is not None
    assert isinstance(cc.summarizer, LlmEventSummarizer)
    assert cc.summarizer._llm.model == "openai/cheap-stub"
    print("OK")


def test_compaction_config_validator_orphan_param():
    """token_threshold without event_retention_size → clear startup error."""
    print("test_compaction_config_validator_orphan_param: ", end="")
    os.environ["ADK_CC_COMPACTION_TOKEN_THRESHOLD"] = "5000"
    _clear_env("ADK_CC_COMPACTION_EVENT_RETENTION",
               "ADK_CC_COMPACTION_INTERVAL", "ADK_CC_COMPACTION_OVERLAP")
    _reset_modules()
    try:
        from adk_cc.agent import app
        print("FAIL: expected RuntimeError for orphan threshold param")
        sys.exit(1)
    except RuntimeError as e:
        assert "must be set together" in str(e)
        print("OK")


def main():
    test_disabled_when_max_unset()
    test_no_action_below_warn()
    test_warn_logs_no_mutation()
    test_reject_short_circuits()
    test_token_counter_fallback()
    test_absolute_overrides()
    test_compaction_config_unset_returns_none()
    test_compaction_config_token_threshold()
    test_compaction_dedicated_model()
    test_compaction_config_validator_orphan_param()
    print()
    print("All context-guard tests passed")


if __name__ == "__main__":
    main()
