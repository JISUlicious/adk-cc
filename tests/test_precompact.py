"""Pre-invocation compaction: measured trigger, safe append, degradation.

The incident this defends: a poisoned session whose last model-reported
usage (76,349) sat under the compaction threshold while every future turn
would replay ~289k tokens — post-turn compaction would never fire, every
turn would overflow. The plugin must trigger on MEASURED payload size,
append the summarizer's compaction event before the turn, and never block
a turn on any failure.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_precompact.py
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

from google.genai import types  # noqa: E402

import adk_cc.agent as agent_mod  # noqa: E402
from adk_cc.permissions.token_counter import estimate_events_tokens  # noqa: E402
from adk_cc.plugins.precompact import PrecompactPlugin  # noqa: E402

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _ev(text="", payload=None, ts=1.0, compaction=None):
    parts = []
    if text:
        parts.append(types.Part(text=text))
    if payload is not None:
        parts.append(types.Part(function_response=types.FunctionResponse(
            id="c1", name="web_fetch", response=payload)))
    actions = SimpleNamespace(compaction=compaction)
    content = SimpleNamespace(parts=parts) if parts else None
    return SimpleNamespace(content=content, actions=actions, timestamp=ts)


class _Svc:
    def __init__(self):
        self.appended = []

    async def append_event(self, session, event):
        self.appended.append(event)
        session.events.append(event)


class _FakeSummarizer:
    def __init__(self, event="COMPACTION-EVENT"):
        self.event = event
        self.calls = 0

    async def maybe_summarize_events(self, *, events):
        self.calls += 1
        self.seen = list(events)
        return self.event


def _ictx(events):
    session = SimpleNamespace(id="s1", events=list(events))
    svc = _Svc()
    return SimpleNamespace(session=session, session_service=svc), svc


def _run(plugin, ictx):
    asyncio.run(plugin.before_run_callback(invocation_context=ictx))


def _with_summarizer(fake):
    orig = agent_mod._make_compaction_summarizer
    agent_mod._make_compaction_summarizer = lambda: fake
    return orig


def main() -> int:
    os.environ["ADK_CC_COMPACTION_TOKEN_THRESHOLD"] = "1000"
    big = [_ev(payload={"content": "x" * 4000}, ts=float(i)) for i in range(12)]
    small = [_ev(text="hi", ts=float(i)) for i in range(12)]

    # --- estimator unit: payloads counted, compaction range respected ----
    est_all = estimate_events_tokens(big)
    check("payloads count toward the measured size", est_all > 10_000, est_all)
    comp = SimpleNamespace(end_timestamp=8.0, compacted_content="tiny summary")
    with_comp = big + [_ev(ts=9.5, compaction=comp)]
    est_after = estimate_events_tokens(with_comp)
    check("events inside the compacted range are not double-counted",
          est_after < est_all / 2, (est_after, est_all))

    # --- above threshold -> summarize head, append, keep tail ------------
    fake = _FakeSummarizer()
    orig = _with_summarizer(fake)
    try:
        ictx, svc = _ictx(big)
        _run(PrecompactPlugin(), ictx)
        check("summarizer invoked once over the HEAD (tail stays)",
              fake.calls == 1 and len(fake.seen) == len(big) - 6,
              getattr(fake, "seen", None) and len(fake.seen))
        check("the compaction event is appended to the session",
              svc.appended == ["COMPACTION-EVENT"], svc.appended)

        # --- under threshold -> untouched --------------------------------
        fake2 = _FakeSummarizer()
        agent_mod._make_compaction_summarizer = lambda: fake2
        ictx2, svc2 = _ictx(small)
        _run(PrecompactPlugin(), ictx2)
        check("a small session is untouched",
              fake2.calls == 0 and svc2.appended == [])

        # --- summarizer declines (None) -> no append, turn proceeds ------
        fake3 = _FakeSummarizer(event=None)
        agent_mod._make_compaction_summarizer = lambda: fake3
        ictx3, svc3 = _ictx(big)
        _run(PrecompactPlugin(), ictx3)
        check("a declining summarizer appends nothing",
              fake3.calls == 1 and svc3.appended == [])

        # --- summarizer raising must not block the turn -------------------
        class _Boom:
            async def maybe_summarize_events(self, *, events):
                raise RuntimeError("summarizer exploded")

        agent_mod._make_compaction_summarizer = lambda: _Boom()
        ictx4, svc4 = _ictx(big)
        _run(PrecompactPlugin(), ictx4)   # must not raise
        check("a raising summarizer is swallowed", svc4.appended == [])

        # --- kill-switch ---------------------------------------------------
        os.environ["ADK_CC_PRECOMPACT"] = "0"
        try:
            fake5 = _FakeSummarizer()
            agent_mod._make_compaction_summarizer = lambda: fake5
            ictx5, svc5 = _ictx(big)
            _run(PrecompactPlugin(), ictx5)
            check("ADK_CC_PRECOMPACT=0 disables", fake5.calls == 0)
        finally:
            os.environ.pop("ADK_CC_PRECOMPACT", None)

        # --- no threshold configured -> inert ------------------------------
        os.environ.pop("ADK_CC_COMPACTION_TOKEN_THRESHOLD", None)
        fake6 = _FakeSummarizer()
        agent_mod._make_compaction_summarizer = lambda: fake6
        ictx6, svc6 = _ictx(big)
        _run(PrecompactPlugin(), ictx6)
        check("without a compaction threshold the plugin is inert",
              fake6.calls == 0)
    finally:
        agent_mod._make_compaction_summarizer = orig
        os.environ.pop("ADK_CC_COMPACTION_TOKEN_THRESHOLD", None)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
