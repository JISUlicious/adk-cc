"""#128 guided /compact: manual compaction core + pending clamp + route.

Covers: the guide reaching the summarizer's prompt, the #119 pending-call
clamp on the direct-summarizer paths, manual_compact's quiescent flow, and
the /api/compact route contract (busy 409, unknown 404, principal 403).

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_manual_compact.py
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

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _text_ev(text="hello", inv="a"):
    from google.adk.events.event import Event
    from google.genai import types
    return Event(author="user", invocation_id=inv,
                 content=types.Content(role="user", parts=[types.Part(text=text)]))


def _call_ev(cid, name="run_bash", inv="a"):
    from google.adk.events.event import Event
    from google.genai import types
    return Event(author="model", invocation_id=inv,
                 content=types.Content(role="model", parts=[types.Part(
                     function_call=types.FunctionCall(id=cid, name=name, args={}))]))


def _resp_ev(cid, response, name="run_bash", inv="a"):
    from google.adk.events.event import Event
    from google.genai import types
    return Event(author="user", invocation_id=inv,
                 content=types.Content(role="user", parts=[types.Part(
                     function_response=types.FunctionResponse(
                         id=cid, name=name, response=response))]))


class _Svc:
    def __init__(self):
        self.appended = []

    async def append_event(self, session, event):
        self.appended.append(event)
        session.events.append(event)


class _StubSummarizerMod:
    """Injected as adk_cc.agent for precompact's lazy import."""

    class _S:
        def __init__(self, guide=None):
            self.guide = guide

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

    def __init__(self):
        self.guides = []

    def _make_compaction_summarizer(self, guide=None):
        self.guides.append(guide)
        return self._S(guide)


def main() -> int:
    from adk_cc.plugins import precompact

    # ---- pending clamp (#119 on the direct paths) ------------------------
    parked = [
        _text_ev("t0"), _text_ev("t1"),
        _call_ev("c-parked"),
        _resp_ev("c-parked", {"status": "needs_confirmation"}),  # interim
        _text_ev("t2"), _text_ev("t3"), _text_ev("t4"),
    ]
    n = precompact._clamp_head_pending(parked, 6)
    check("clamp stops BEFORE the parked call (interim response ignored)",
          n == 2, n)
    answered = [
        _text_ev("t0"), _call_ev("c-done"),
        _resp_ev("c-done", {"stdout": "ok"}),
        _text_ev("t1"), _text_ev("t2"),
    ]
    check("really-answered call does not clamp",
          precompact._clamp_head_pending(answered, 4) == 4)

    # ---- manual_compact core --------------------------------------------
    stub_mod = _StubSummarizerMod()
    had = sys.modules.get("adk_cc.agent")
    sys.modules["adk_cc.agent"] = stub_mod
    try:
        events = [_text_ev(f"e{i}" + "x" * 200) for i in range(10)]
        session = SimpleNamespace(id="s1", events=list(events))
        svc = _Svc()
        r = asyncio.run(precompact.manual_compact(session, svc, "keep #127"))
        check("manual_compact summarizes and reports",
              r["status"] == "summarized" and r["compacted_events"] == 4
              and r["guided"] is True, r)
        check("guide reaches the summarizer factory",
              stub_mod.guides == ["keep #127"], stub_mod.guides)
        check("compaction event appended", len(svc.appended) == 1)

        small = SimpleNamespace(id="s2", events=[_text_ev("a"), _text_ev("b")])
        r2 = asyncio.run(precompact.manual_compact(small, _Svc(), None))
        check("small session: nothing_to_compact",
              r2["status"] == "nothing_to_compact", r2)
    finally:
        if had is not None:
            sys.modules["adk_cc.agent"] = had
        else:
            sys.modules.pop("adk_cc.agent", None)

    # ---- guide threading into the REAL summarizer prompt ----------------
    from adk_cc.agent import _make_compaction_summarizer
    plain = _make_compaction_summarizer()
    guided = _make_compaction_summarizer(guide="keep #127, drop 125/126")
    check("real factory: guide lands in the prompt template",
          "keep #127, drop 125/126" in (guided.prompt_template or "")
          and "keep #127" not in (plain.prompt_template or ""))
    check("real factory: guidance never licenses dropping pinned work",
          "NEVER omit unfinished work" in (guided.prompt_template or ""))

    # ---- /api/compact route contract ------------------------------------
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from adk_cc.service.turn_routes import mount_turn_routes
    from adk_cc.plugins import precompact as pc

    session_obj = SimpleNamespace(id="sX", events=[])

    class _SvcB:
        async def get_session(self, *, app_name, user_id, session_id):
            return session_obj if session_id == "sX" else None

    class _Broker:
        session_service = _SvcB()
        busy = False

        def latest_for(self, a, u, s):
            return SimpleNamespace(id="t1", status="running") if self.busy else None

        def start(self, **kw):
            raise RuntimeError("unused")

    broker = _Broker()
    app = FastAPI()
    mount_turn_routes(app, broker)
    client = TestClient(app)
    body = {"appName": "adk_cc", "userId": "u1", "sessionId": "sX",
            "guide": "keep the important bits"}

    seen = []

    async def fake_manual(session, svc, guide=None):
        seen.append((session, guide))
        return {"status": "summarized", "before_tokens": 100,
                "after_tokens": 40, "compacted_events": 3, "guided": True}

    real_manual = pc.manual_compact
    pc.manual_compact = fake_manual
    try:
        r = client.post("/api/compact", json=body)
        check("route: quiescent session compacts (200 + payload)",
              r.status_code == 200 and r.json()["status"] == "summarized",
              (r.status_code, r.text[:120]))
        check("route: guide forwarded",
              seen and seen[-1][1] == "keep the important bits", seen)

        broker.busy = True
        r2 = client.post("/api/compact", json=body)
        check("route: busy session → 409", r2.status_code == 409, r2.status_code)
        broker.busy = False

        r3 = client.post("/api/compact", json={**body, "sessionId": "nope"})
        check("route: unknown session → 404", r3.status_code == 404, r3.status_code)

        r4 = client.post("/api/compact", json={"appName": "adk_cc"})
        check("route: missing ids → 400", r4.status_code == 400, r4.status_code)
    finally:
        pc.manual_compact = real_manual

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
