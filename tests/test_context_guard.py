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


def _build_request(*, target_tokens: int = 0, system_instruction: Optional[str] = None,
                   model: str = "openai/gpt-4"):
    """Build a fake LlmRequest whose chars/4 estimate equals `target_tokens`.

    Plugin threshold checks (PR #23 / token-counter unification) use
    ADK's shared chars/4 estimator. Tests target that algorithm
    directly: a text of `target_tokens * 4` characters yields exactly
    `target_tokens` under chars/4."""
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    text = "x" * (target_tokens * 4) if target_tokens > 0 else ""
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


def test_chars_div_4_threshold_path():
    """Threshold check uses ADK's chars/4 algorithm — 4000 chars
    yields exactly 1000 tokens, crossing REJECT (950 = 95% of 1000)."""
    print("test_chars_div_4_threshold_path: ", end="")
    os.environ["ADK_CC_MAX_CONTEXT_TOKENS"] = "1000"
    _clear_env("ADK_CC_CONTEXT_WARN_TOKENS", "ADK_CC_CONTEXT_REJECT_TOKENS")
    _reset_modules()
    from adk_cc.plugins.context_guard import ContextGuardPlugin
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    # 4000 chars / 4 = 1000 tokens → over REJECT.
    text = "x" * 4000
    req = LlmRequest(
        model="openai/gpt-4",
        contents=[types.Content(role="user", parts=[types.Part(text=text)])],
        config=types.GenerateContentConfig(),
    )
    plugin = ContextGuardPlugin()
    result = asyncio.run(plugin.before_model_callback(
        callback_context=_FakeCallbackContext(), llm_request=req,
    ))
    assert result is not None, "threshold check should fire REJECT"
    print("OK")


def test_uses_usage_metadata_when_present():
    """When session events carry `usage_metadata.prompt_token_count`,
    the plugin uses that (model's own count from a prior response)
    instead of the chars/4 estimate. This mirrors ADK's
    `_latest_prompt_token_count` algorithm — unifying the two layers."""
    print("test_uses_usage_metadata_when_present: ", end="")
    os.environ["ADK_CC_MAX_CONTEXT_TOKENS"] = "10000"
    _clear_env("ADK_CC_CONTEXT_WARN_TOKENS", "ADK_CC_CONTEXT_REJECT_TOKENS")
    _reset_modules()
    from adk_cc.plugins.context_guard import ContextGuardPlugin
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    # Small text (chars/4 = ~10 tokens) but usage_metadata reports 9600 tokens.
    # If the plugin honors usage_metadata, REJECT fires (9600 >= 9500).
    # If it falls back to chars/4 by mistake, the request would slip through.
    req = LlmRequest(
        model="openai/gpt-4",
        contents=[types.Content(role="user", parts=[types.Part(text="short")])],
        config=types.GenerateContentConfig(),
    )

    class _FakeUsage:
        prompt_token_count = 9600

    class _FakeEvent:
        usage_metadata = _FakeUsage()

    class _FakeSessionWithEvents:
        id = "sess-X"
        events = [_FakeEvent()]

    class _FakeCtxWithEvents:
        @property
        def session(self): return _FakeSessionWithEvents()

    plugin = ContextGuardPlugin()
    result = asyncio.run(plugin.before_model_callback(
        callback_context=_FakeCtxWithEvents(), llm_request=req,
    ))
    assert result is not None, "should REJECT based on usage_metadata count"
    print("OK")


def test_agrees_with_adk_estimate_when_no_metadata():
    """The shared helper's chars/4 algorithm matches ADK's per-content
    counter byte-for-byte. Both layers' total then depends on which
    content list they're handed:

      - ADK's `_estimate_prompt_token_count` runs `_get_contents()` to
        produce an effective-content list (filters by branch / agent),
        then sums `_count_text_chars_in_content` over it.
      - Our `estimate_prompt_tokens` sums the same `_count_text_chars_in_content`
        over `llm_request.contents` (already built by the time our
        plugin fires).

    So full-pipeline agreement requires both layers to see the same
    inputs. The ALGORITHM agreement is what unification fixes — verify
    by feeding the SAME content list to both estimators."""
    print("test_agrees_with_adk_estimate_when_no_metadata: ", end="")
    _clear_env("ADK_CC_MAX_CONTEXT_TOKENS",
               "ADK_CC_CONTEXT_WARN_TOKENS", "ADK_CC_CONTEXT_REJECT_TOKENS")
    _reset_modules()
    from adk_cc.permissions.token_counter import (
        _count_text_chars_in_content,
        estimate_prompt_tokens,
    )
    from google.adk.apps.compaction import _count_text_chars_in_content as adk_count
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    # Per-content char count agrees with ADK's, byte-for-byte.
    cases = [
        "hello world",
        "x" * 1000,
        "multi\nline\ntext",
        "",
        "unicode: ñ é ü 中文 emoji 🚀",
    ]
    for txt in cases:
        c = types.Content(role="user", parts=[types.Part(text=txt)])
        assert _count_text_chars_in_content(c) == adk_count(c), (
            f"per-content count diverges for {txt!r}: "
            f"ours={_count_text_chars_in_content(c)} adk={adk_count(c)}"
        )

    # Full estimator: given the same content list (no usage_metadata),
    # chars/4 sum is identical to what ADK gets over the same list.
    contents = [
        types.Content(role="user",
                      parts=[types.Part(text="hello world " * 50)]),
        types.Content(role="model",
                      parts=[types.Part(text="response text " * 30)]),
    ]
    req = LlmRequest(
        model="openai/gpt-4",
        contents=contents,
        config=types.GenerateContentConfig(),
    )
    ours = estimate_prompt_tokens(req, session_events=None)
    theirs_chars = sum(adk_count(c) for c in contents)
    theirs = theirs_chars // 4
    assert ours == theirs, f"ours={ours} theirs={theirs}"
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
    """Threshold + retention set, no dedicated compaction model.
    Our wrapper is ALWAYS installed (it falls back to the main-agent
    model env vars), so audit + DEBUG hooks fire regardless of whether
    the operator set ADK_CC_COMPACTION_MODEL. The summarizer's
    `model_id` resolves from ADK_CC_MODEL (or the gpt-4 last-resort
    fallback when both are unset)."""
    print("test_compaction_config_token_threshold: ", end="")
    os.environ["ADK_CC_COMPACTION_TOKEN_THRESHOLD"] = "5000"
    os.environ["ADK_CC_COMPACTION_EVENT_RETENTION"] = "8"
    _clear_env("ADK_CC_COMPACTION_INTERVAL", "ADK_CC_COMPACTION_OVERLAP",
               "ADK_CC_COMPACTION_MODEL")
    _reset_modules()
    from adk_cc.agent import app
    from google.adk.apps.base_events_summarizer import BaseEventsSummarizer
    cc = app.events_compaction_config
    assert cc is not None
    assert cc.token_threshold == 5000
    assert cc.event_retention_size == 8
    # Wrapper installed even without dedicated compaction model.
    assert isinstance(cc.summarizer, BaseEventsSummarizer)
    # Model id falls back to main-agent ADK_CC_MODEL (or gpt-4 if also unset).
    expected_model = os.environ.get("ADK_CC_MODEL") or "openai/gpt-4"
    assert cc.summarizer.model_id == expected_model
    print("OK")


def test_compaction_dedicated_model():
    print("test_compaction_dedicated_model: ", end="")
    os.environ["ADK_CC_COMPACTION_TOKEN_THRESHOLD"] = "5000"
    os.environ["ADK_CC_COMPACTION_EVENT_RETENTION"] = "8"
    os.environ["ADK_CC_COMPACTION_MODEL"] = "openai/cheap-stub"
    _reset_modules()
    from adk_cc.agent import app
    from google.adk.apps.base_events_summarizer import BaseEventsSummarizer

    cc = app.events_compaction_config
    assert cc is not None
    # Lazy summarizer stores config strings only — never a LiteLlm — so the
    # surrounding config stays JSON-serializable. Real LlmEventSummarizer
    # is constructed per-compaction call.
    assert isinstance(cc.summarizer, BaseEventsSummarizer)
    assert cc.summarizer.model_id == "openai/cheap-stub"
    # Critical: the config + app must be JSON-serializable. This was the
    # bug — a LiteLlm sitting on summarizer._llm leaked LiteLLMClient into
    # pydantic's dump_json and crashed FastAPI's serialize_response.
    s = cc.model_dump_json(exclude_none=True)
    assert "openai/cheap-stub" in s
    # api_key field excluded from dumps so it doesn't leak into logs / traces.
    assert "api_key" not in s
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
    test_chars_div_4_threshold_path()
    test_uses_usage_metadata_when_present()
    test_agrees_with_adk_estimate_when_no_metadata()
    test_absolute_overrides()
    test_compaction_config_unset_returns_none()
    test_compaction_config_token_threshold()
    test_compaction_dedicated_model()
    test_compaction_config_validator_orphan_param()
    print()
    print("All context-guard tests passed")


if __name__ == "__main__":
    main()
