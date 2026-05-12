"""Inner script for token_counter_demo.py — runs inside a subprocess
so the agent's `configure_logging()` at import time picks up our
env vars."""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

# Importing the agent applies configure_logging() with our env.
from adk_cc import agent  # noqa: F401
from adk_cc.permissions.token_counter import (
    _count_text_chars_in_content,
    estimate_prompt_tokens,
)
from adk_cc.plugins.context_guard import ContextGuardPlugin
from google.adk.apps.compaction import (
    _count_text_chars_in_content as adk_count,
    _estimate_prompt_token_count as adk_estimate,
)
from google.adk.events.event import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


# --- Fakes for callback_context.session.events --------------------


class _FakeUsage:
    def __init__(self, n: int) -> None:
        self.prompt_token_count = n


class _FakeEvent:
    def __init__(self, n: Optional[int]) -> None:
        self.usage_metadata = _FakeUsage(n) if n is not None else None


class _FakeSession:
    def __init__(self, sid: str, events: Optional[list] = None) -> None:
        self.id = sid
        self.events = events or []


class _FakeCtx:
    def __init__(self, sid: str, events: Optional[list] = None) -> None:
        self._session = _FakeSession(sid, events)

    @property
    def session(self):
        return self._session


def _build_request(text: str) -> LlmRequest:
    return LlmRequest(
        model="openai/demo-model",
        contents=[types.Content(role="user", parts=[types.Part(text=text)])],
        config=types.GenerateContentConfig(),
    )


async def scenario_1_chars_div_4() -> None:
    """Plain chars/4 path. Request carries 3200 chars → 800 tokens →
    over WARN (750). Plugin should WARN, AND emit the DEBUG
    comparison line showing both shared and litellm counts."""
    print("\n[scenario 1] chars/4 path: 3200 chars (= 800 tokens by chars/4)")
    print("  expect: WARN log, DEBUG comparison line, result=None (not REJECT yet)")
    plugin = ContextGuardPlugin()
    req = _build_request("x" * 3200)
    result = await plugin.before_model_callback(
        callback_context=_FakeCtx("sess-chars"), llm_request=req
    )
    print(f"  result type: {type(result).__name__}  (None means continue)")


async def scenario_2_usage_metadata() -> None:
    """usage_metadata path. Request body is short (~10 tokens), but
    the latest session event reports prompt_token_count=960 — over
    REJECT. Plugin uses usage_metadata; REJECTs."""
    print("\n[scenario 2] usage_metadata path: short text + prior event reports 960 tokens")
    print("  expect: REJECT log, plugin returns LlmResponse (short-circuits)")
    plugin = ContextGuardPlugin()
    req = _build_request("short text — chars/4 here would be tiny")
    events = [_FakeEvent(100), _FakeEvent(500), _FakeEvent(960)]
    result = await plugin.before_model_callback(
        callback_context=_FakeCtx("sess-meta", events), llm_request=req
    )
    print(f"  result type: {type(result).__name__}  (LlmResponse means REJECT fired)")
    if isinstance(result, LlmResponse) and result.content:
        text = "".join(p.text or "" for p in (result.content.parts or []))
        print(f"  REJECT text: {text!r}")


async def scenario_3_no_action_below_warn() -> None:
    """Under-WARN sanity. 2000 chars = 500 tokens, under 750 WARN.
    No log, plugin returns None."""
    print("\n[scenario 3] under-threshold: 2000 chars (= 500 tokens by chars/4)")
    print("  expect: no WARN/REJECT, just the DEBUG comparison line, result=None")
    plugin = ContextGuardPlugin()
    req = _build_request("x" * 2000)
    result = await plugin.before_model_callback(
        callback_context=_FakeCtx("sess-quiet"), llm_request=req
    )
    print(f"  result type: {type(result).__name__}")


def scenario_4_algorithm_parity() -> None:
    """Algorithm parity: feed the SAME content list to ADK's per-content
    counter and ours. Demonstrate byte-for-byte agreement across a
    range of inputs (the whole point of PR C)."""
    print("\n[scenario 4] algorithm parity vs ADK's `_count_text_chars_in_content`")
    cases = [
        ("ascii", "hello world"),
        ("empty", ""),
        ("multiline", "line one\nline two\nline three"),
        ("unicode", "ñ é ü 中文 🚀 emoji"),
        ("long ascii", "x" * 10_000),
        ("mixed", "tabs\tand\tnewlines\nand 中文 mixed"),
    ]
    print(f"  {'case':<14} {'ours':>8} {'adk':>8} {'match':>6}")
    print(f"  {'-' * 14} {'-' * 8:>8} {'-' * 8:>8} {'-' * 6:>6}")
    all_match = True
    for label, txt in cases:
        c = types.Content(role="user", parts=[types.Part(text=txt)])
        ours = _count_text_chars_in_content(c)
        theirs = adk_count(c)
        match = "✓" if ours == theirs else "✗"
        if ours != theirs:
            all_match = False
        print(f"  {label:<14} {ours:>8} {theirs:>8} {match:>6}")
    print(f"  parity: {'all match' if all_match else 'DIVERGENCE'}")


def scenario_5_full_pipeline_parity() -> None:
    """Full estimator parity: same Event list → same chars/4 result.
    Compares our estimate_prompt_tokens (request-based) against ADK's
    _estimate_prompt_token_count (events-based) when given the same
    text content."""
    print("\n[scenario 5] full-pipeline parity")
    events = [
        Event(
            invocation_id="i", author="user",
            content=types.Content(role="user",
                                  parts=[types.Part(text="user said this " * 50)]),
        ),
        Event(
            invocation_id="i", author="agent",
            content=types.Content(role="model",
                                  parts=[types.Part(text="model replied that " * 30)]),
        ),
    ]
    req = LlmRequest(
        model="openai/demo-model",
        contents=[e.content for e in events],
        config=types.GenerateContentConfig(),
    )
    # No usage_metadata → both fall back to chars/4 on their own
    # content lists.
    ours = estimate_prompt_tokens(req, session_events=events)
    # Pass the same agent_name as the Event author so ADK's content
    # filtering keeps both events (different filters apply for
    # cross-agent visibility).
    theirs = adk_estimate(events=events, current_branch=None, agent_name="agent") or 0
    print(f"  estimate_prompt_tokens(req) = {ours}")
    print(f"  ADK._estimate_prompt_token_count(events, agent='agent') = {theirs}")
    print(f"  delta: {abs(ours - theirs)} tokens "
          f"(small deltas come from ADK's pre-filter — algorithm itself agrees)")


async def main_async() -> int:
    print(f"\n=== ContextGuardPlugin scenarios ===")
    print(f"(plugin construction logs the resolved MAX/WARN/REJECT at INFO)")
    await scenario_1_chars_div_4()
    await scenario_2_usage_metadata()
    await scenario_3_no_action_below_warn()
    scenario_4_algorithm_parity()
    scenario_5_full_pipeline_parity()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
