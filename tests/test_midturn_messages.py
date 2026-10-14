"""Mid-turn messages + send-now, on a REAL ADK Runner over the real adk_cc App
(scripted model, no network).

Covers:
  - a message queued while the model is busy is DELIVERED at the next model
    step — after the tool result, never between call and response — persisted
    as a user event and visible to the model in that very request;
  - a message that arrives after the last model step becomes a FOLLOW-UP turn
    (nothing is lost), and the old turn names it;
  - an idle session: ifBusy=queue simply starts a turn;
  - send-now: the running turn is stopped and the new message runs, carrying
    any messages still queued on the stopped turn;
  - withdrawing a queued message before delivery.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_midturn_messages.py
"""
from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
_TMP = tempfile.mkdtemp(prefix="adk-cc-midturn-")
os.environ.setdefault("ADK_CC_DESKTOP", "1")
os.environ.setdefault("ADK_CC_DESKTOP_DATA", _TMP)
os.environ.setdefault("ADK_CC_WORKSPACE_ROOT", _TMP)
os.environ.pop("ADK_CC_MEMORY", None)

from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402
from pydantic import Field  # noqa: E402

from adk_cc.service.turns import TurnBroker  # noqa: E402

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _text(t: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=t)]),
                       partial=False)


def _call(name: str, args: dict, cid: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(name=name, args=args, id=cid))]), partial=False)


class _Llm(BaseLlm):
    """Scripted model; call i waits on gates[i] when present; records every
    request's contents so tests can see what the model was shown."""
    model: str = "fake/midturn"
    responses: list = Field(default_factory=list)
    gates: list = Field(default_factory=list)
    entered: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)
    calls: int = 0

    async def generate_content_async(self, req, stream: bool = False):
        i = self.calls
        self.calls += 1
        self.seen.append(list(req.contents))
        if i < len(self.entered):
            self.entered[i].set()
        if i < len(self.gates):
            await self.gates[i].wait()
        yield self.responses.pop(0)


def _fresh(responses, gates=(), entered=()):
    import adk_cc.agent as A

    llm = _Llm(responses=list(responses), gates=list(gates), entered=list(entered))
    A.MODEL._resolve_delegate = lambda: llm
    svc = InMemorySessionService()
    runner = Runner(app=A.app, session_service=svc,
                    artifact_service=None, memory_service=None)

    async def get_runner(_app):
        return runner

    return TurnBroker(get_runner=get_runner, session_service=svc), svc, llm


async def _session(svc) -> str:
    import adk_cc.agent as A

    s = await svc.create_session(app_name=A.app.name, user_id="u1",
                                 state={"session_title": "scripted"})
    return s.id


def _msg(t):
    return types.Content(role="user", parts=[types.Part(text=t)])


def _texts(contents) -> list[str]:
    out = []
    for c in contents:
        for p in (c.parts or []):
            if getattr(p, "text", None):
                out.append(p.text)
    return out


async def test_delivered_at_next_model_step():
    g0, e0 = asyncio.Event(), asyncio.Event()
    broker, svc, llm = _fresh(
        [_call("read_file", {"path": "nope.txt"}, "c1"), _text("ok, switching")],
        gates=[g0], entered=[e0])
    sid = await _session(svc)
    turn = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                        new_message=_msg("read nope.txt"))
    await asyncio.wait_for(e0.wait(), 10)          # model call 1 in flight
    kind, t2, item = await broker.enqueue(app_name="adk_cc", user_id="u1",
                                          session_id=sid, text="use docs/ instead")
    check("busy session → message queued on the running turn",
          kind == "queued" and t2 is turn and item and turn.queue == [item])
    g0.set()
    await asyncio.wait_for(turn.task, 30)
    check("turn completed", turn.status == "done", turn.snapshot())
    check("the model's NEXT request carried the message",
          llm.calls == 2 and any("use docs/ instead" in t for t in _texts(llm.seen[1])),
          _texts(llm.seen[1])[-3:] if len(llm.seen) > 1 else llm.seen)
    last = llm.seen[1][-1]
    check("…as the final content, framed as sent-while-working",
          last.role == "user" and "while you were working" in (last.parts[0].text or ""))
    # ordering: the tool response precedes the injected message
    roles = [(c.role, bool(c.parts and c.parts[0].function_response)) for c in llm.seen[1]]
    fr_idx = max(i for i, (_r, fr) in enumerate(roles) if fr)
    check("…after the tool result, never between call and response",
          fr_idx == len(roles) - 2, roles)
    s = await svc.get_session(app_name="adk_cc", user_id="u1", session_id=sid)
    users = [e for e in s.events if e.author == "user"
             and (e.custom_metadata or {}).get("adk_cc_midturn")]
    check("persisted as a user event under the same invocation",
          len(users) == 1 and users[0].invocation_id == s.events[0].invocation_id)
    check("…with the framing PERSISTED (survives to later steps)",
          users and "while you were working" in users[0].content.parts[0].text
          and "use docs/ instead" in users[0].content.parts[0].text)
    check("…and the user's raw text kept for the UI",
          users and users[0].custom_metadata.get("adk_cc_midturn_raw") == "use docs/ instead")
    check("streamed to the turn's tail", any("use docs/ instead" in p for p in turn.events))
    check("queue drained, no follow-up", turn.queue == [] and turn.followup_id is None)


async def test_late_message_becomes_followup():
    g0, e0 = asyncio.Event(), asyncio.Event()
    broker, svc, llm = _fresh([_text("all done"), _text("handled the late one")],
                              gates=[g0], entered=[e0])
    sid = await _session(svc)
    turn = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                        new_message=_msg("hi"))
    await asyncio.wait_for(e0.wait(), 10)          # the LAST model call
    await broker.enqueue(app_name="adk_cc", user_id="u1", session_id=sid,
                         text="one more thing")
    g0.set()
    await asyncio.wait_for(turn.task, 30)
    check("turn ended with a follow-up named", turn.status == "done"
          and turn.followup_id is not None, turn.snapshot())
    nxt = broker.get(turn.followup_id or "")
    if nxt:
        await asyncio.wait_for(nxt.task, 30)
    check("follow-up ran the late message",
          nxt is not None and nxt.status == "done"
          and any("one more thing" in t for t in _texts(llm.seen[-1])))
    check("snapshot exposes the follow-up id",
          turn.snapshot().get("followup_turn_id") == turn.followup_id)


async def test_idle_session_queue_starts_turn():
    broker, svc, _llm = _fresh([_text("hello")])
    sid = await _session(svc)
    kind, turn, item = await broker.enqueue(app_name="adk_cc", user_id="u1",
                                            session_id=sid, text="hi")
    check("idle session → a normal turn starts", kind == "started" and item is None)
    await asyncio.wait_for(turn.task, 30)
    check("and completes", turn.status == "done")


async def test_send_now_interrupts_and_carries_queue():
    g0, e0 = asyncio.Event(), asyncio.Event()      # g0 never released
    broker, svc, llm = _fresh([_text("never"), _text("did the new thing")],
                              gates=[g0], entered=[e0])
    sid = await _session(svc)
    first = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                         new_message=_msg("slow task"))
    await asyncio.wait_for(e0.wait(), 10)
    await broker.enqueue(app_name="adk_cc", user_id="u1", session_id=sid,
                         text="queued before send-now")
    nxt = await broker.start_interrupting(app_name="adk_cc", user_id="u1",
                                          session_id=sid,
                                          new_message=_msg("stop, do this"))
    check("the running turn was stopped", first.status == "aborted", first.status)
    await asyncio.wait_for(nxt.task, 30)
    seen = _texts(llm.seen[-1])
    check("the new message ran", nxt.status == "done" and "stop, do this" in seen)
    check("queued messages were carried, not dropped",
          "queued before send-now" in seen, seen[-4:])


async def test_withdraw_before_delivery():
    g0, e0 = asyncio.Event(), asyncio.Event()
    broker, svc, llm = _fresh([_text("done")], gates=[g0], entered=[e0])
    sid = await _session(svc)
    turn = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                        new_message=_msg("hi"))
    await asyncio.wait_for(e0.wait(), 10)
    _k, _t, item = await broker.enqueue(app_name="adk_cc", user_id="u1",
                                        session_id=sid, text="never mind")
    turn.queue = [q for q in turn.queue if q["id"] != item["id"]]   # route's op
    g0.set()
    await asyncio.wait_for(turn.task, 30)
    check("withdrawn message produced no follow-up",
          turn.followup_id is None and llm.calls == 1)


async def test_interrupt_mid_tool_closes_the_call():
    """Stopping a turn while a tool RUNS left the call unanswered and the
    next model request was rejected ("No tool output found for function
    call …"). The abort must answer it as interrupted."""
    import adk_cc.agent as A

    broker, svc, llm = _fresh([_call("run_bash", {"command": "sleep 30"}, "slow1"),
                               _text("ok, new task done")])
    s = await svc.create_session(app_name=A.app.name, user_id="u1", state={
        "session_title": "scripted", "permission_mode": "bypassPermissions"})
    first = broker.start(app_name="adk_cc", user_id="u1", session_id=s.id,
                         new_message=_msg("run sleep 30"))
    for _ in range(100):
        if first.current_tool == "run_bash":
            break
        await asyncio.sleep(0.1)
    check("the slow tool is actually running", first.current_tool == "run_bash",
          first.current_tool)
    nxt = await broker.start_interrupting(app_name="adk_cc", user_id="u1",
                                          session_id=s.id,
                                          new_message=_msg("stop, do this"))
    await asyncio.wait_for(nxt.task, 30)
    check("the follow-on turn succeeded (no rejected request)",
          nxt.status == "done", nxt.snapshot())
    frs = [p.function_response for c in llm.seen[-1] for p in (c.parts or [])
           if getattr(p, "function_response", None)]
    closed = [fr for fr in frs if fr.id == "slow1"]
    check("the interrupted call was answered as interrupted",
          len(closed) == 1 and (closed[0].response or {}).get("status") == "interrupted",
          frs)


async def test_framing_survives_later_steps():
    """Delivered during step 1, the message must still read as mid-turn at
    step 3 — the old request-only frame was gone one call later."""
    g0, e0 = asyncio.Event(), asyncio.Event()
    broker, svc, llm = _fresh(
        [_call("read_file", {"path": "a.txt"}, "c1"),
         _call("read_file", {"path": "b.txt"}, "c2"),
         _call("read_file", {"path": "c.txt"}, "c3"),
         _text("done, and handled the late note")],
        gates=[g0], entered=[e0])
    sid = await _session(svc)
    turn = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                        new_message=_msg("read three files"))
    await asyncio.wait_for(e0.wait(), 10)
    await broker.enqueue(app_name="adk_cc", user_id="u1", session_id=sid,
                         text="LATE-NOTE also summarize them")
    g0.set()
    await asyncio.wait_for(turn.task, 30)
    # The coordinator's LAST step: the latest request that carries the third
    # tool result (other plugins, e.g. memory capture, may call the model too).
    coord = [r for r in llm.seen
             if any(getattr(p, "function_response", None)
                    and p.function_response.id == "c3"
                    for c in r for p in (c.parts or []))]
    last_req = _texts(coord[-1]) if coord else []
    hit = [t for t in last_req if "LATE-NOTE" in t]
    check("three steps later the message is still framed as mid-turn",
          len(hit) == 1 and "while you were working" in hit[0]
          and "Never silently ignore" in hit[0], hit)


def main() -> int:
    for t in (test_delivered_at_next_model_step, test_late_message_becomes_followup,
              test_idle_session_queue_starts_turn,
              test_send_now_interrupts_and_carries_queue,
              test_withdraw_before_delivery,
              test_interrupt_mid_tool_closes_the_call,
              test_framing_survives_later_steps):
        asyncio.run(t())
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
