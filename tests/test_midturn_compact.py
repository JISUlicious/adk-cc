"""#128 P2: mid-turn compaction of PRIOR-invocation session events.

The hard rules under test: current-invocation events are NEVER candidates
(the active flow — pending tool pairs, parked confirmations — lives
there); a short prior tail is kept; the guard triggers at most once per
invocation and only when the pressure line stays hot after request-layer
rewriting.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_midturn_compact.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
for _k in list(os.environ):
    if _k.startswith(("ADK_CC_MAX_CONTEXT", "ADK_CC_CONTEXT_",
                      "ADK_CC_MIDTURN", "ADK_CC_PRECOMPACT")):
        os.environ.pop(_k)

from adk_cc.plugins import precompact  # noqa: E402

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _ev(inv, text="e"):
    from google.genai import types
    return SimpleNamespace(
        invocation_id=inv, author="user", timestamp=1.0,
        content=types.Content(role="user", parts=[types.Part(text=text)]))


class _Svc:
    def __init__(self):
        self.appended = []

    async def append_event(self, session, event):
        self.appended.append(event)
        session.events.append(event)


def _ctx(events, current="cur"):
    session = SimpleNamespace(id="s1", events=list(events))
    svc = _Svc()
    return SimpleNamespace(
        session=session, invocation_id=current,
        _invocation_context=SimpleNamespace(
            session=session, session_service=svc, invocation_id=current),
    ), svc


def _capture_core(captured):
    async def fake(session, svc, head, **kw):
        captured.append({"head": head, "svc": svc, **kw})
        return True
    return fake


def main() -> int:
    real_core = precompact._summarize_and_append

    # ---- slicing rules ---------------------------------------------------
    captured = []
    precompact._summarize_and_append = _capture_core(captured)
    try:
        # 6 prior events (2 invocations) + 3 current — head = prior minus
        # tail(2); current events NEVER appear.
        events = [_ev("a") for _ in range(3)] + [_ev("b") for _ in range(3)] \
            + [_ev("cur") for _ in range(3)]
        ctx, _ = _ctx(events)
        ok = asyncio.run(precompact.midturn_compact_prior(ctx))
        head = captured[-1]["head"] if captured else []
        check("compacts prior head (prior minus tail of 2)",
              ok and len(head) == 4, (ok, len(head)))
        check("current-invocation events excluded from candidates",
              all(e.invocation_id != "cur" for e in head))
        check("runs in force mode (mechanical fallback guaranteed)",
              captured and captured[-1]["force"] is True)

        # All events belong to the current invocation → nothing to compact.
        captured.clear()
        ctx2, _ = _ctx([_ev("cur") for _ in range(12)])
        ok2 = asyncio.run(precompact.midturn_compact_prior(ctx2))
        check("all-current session: refuses to touch anything",
              ok2 is False and not captured)

        # Too few prior events → skip (min head 4 after tail 2).
        captured.clear()
        ctx3, _ = _ctx([_ev("a") for _ in range(5)] + [_ev("cur")])
        ok3 = asyncio.run(precompact.midturn_compact_prior(ctx3))
        check("tiny prior history: skipped", ok3 is False and not captured)

        # Kill switch.
        captured.clear()
        os.environ["ADK_CC_MIDTURN_COMPACT"] = "0"
        try:
            ctx4, _ = _ctx(events)
            ok4 = asyncio.run(precompact.midturn_compact_prior(ctx4))
            check("ADK_CC_MIDTURN_COMPACT=0 disables", ok4 is False and not captured)
        finally:
            os.environ.pop("ADK_CC_MIDTURN_COMPACT", None)
    finally:
        precompact._summarize_and_append = real_core

    # ---- shared core appends through the session service ----------------
    class _StubSummarizer:
        async def maybe_summarize_events(self, *, events, force=False):
            from google.adk.events.event import Event
            from google.adk.events.event_actions import EventActions, EventCompaction
            from google.genai import types
            return Event(author="user", invocation_id=Event.new_id(),
                         actions=EventActions(compaction=EventCompaction(
                             start_timestamp=0.0, end_timestamp=1.0,
                             compacted_content=types.Content(
                                 role="model",
                                 parts=[types.Part(text="SUMMARY")]))))

    agent_stub = SimpleNamespace(
        _make_compaction_summarizer=lambda: _StubSummarizer())
    had_agent = sys.modules.get("adk_cc.agent")
    sys.modules["adk_cc.agent"] = agent_stub  # lazy import target
    try:
        events = [_ev("a") for _ in range(6)] + [_ev("cur")]
        ctx5, svc5 = _ctx(events)
        ok5 = asyncio.run(precompact.midturn_compact_prior(ctx5))
        check("core path: compaction event appended via session service",
              ok5 and len(svc5.appended) == 1
              and svc5.appended[0].actions.compaction is not None)
    finally:
        if had_agent is not None:
            sys.modules["adk_cc.agent"] = had_agent
        else:
            sys.modules.pop("adk_cc.agent", None)

    # ---- guard trigger: once per invocation, only while still hot --------
    os.environ["ADK_CC_MAX_CONTEXT_TOKENS"] = "2000"
    from adk_cc.plugins.context_guard import ContextGuardPlugin
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    calls = []

    async def fake_midturn(cb):
        calls.append(cb)
        return True

    real_midturn = precompact.midturn_compact_prior
    precompact.midturn_compact_prior = fake_midturn
    try:
        guard = ContextGuardPlugin()
        guard._session_events = lambda cb: []
        guard._session_id = lambda cb: "s1"

        def hot_request():  # non-evictable text ~1750 tokens: ladder frees 0
            return LlmRequest(contents=[
                types.Content(role="user", parts=[types.Part(text="x" * 7000)]),
            ])

        cb = SimpleNamespace(invocation_id="inv1")
        r1 = asyncio.run(guard.before_model_callback(
            callback_context=cb, llm_request=hot_request()))
        check("guard fires mid-turn compaction when ladder can't cool it",
              len(calls) == 1 and r1 is None, (len(calls), r1))
        asyncio.run(guard.before_model_callback(
            callback_context=cb, llm_request=hot_request()))
        check("same invocation: not fired twice", len(calls) == 1, len(calls))
        cb2 = SimpleNamespace(invocation_id="inv2")
        asyncio.run(guard.before_model_callback(
            callback_context=cb2, llm_request=hot_request()))
        check("new invocation: eligible again", len(calls) == 2, len(calls))

        # Below the pressure line: never fired.
        calls.clear()
        guard2 = ContextGuardPlugin()
        guard2._session_events = lambda cb: []
        guard2._session_id = lambda cb: "s1"
        cool = LlmRequest(contents=[
            types.Content(role="user", parts=[types.Part(text="x" * 2000)])])
        asyncio.run(guard2.before_model_callback(
            callback_context=SimpleNamespace(invocation_id="inv3"),
            llm_request=cool))
        check("below pressure: mid-turn compaction never invoked",
              not calls, len(calls))
    finally:
        precompact.midturn_compact_prior = real_midturn
        os.environ.pop("ADK_CC_MAX_CONTEXT_TOKENS", None)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
