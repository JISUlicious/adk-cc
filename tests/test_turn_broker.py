"""Turn Broker (durable runs) — P1 tests. No live models, no network.

Covers:
  - a turn runs to completion with NO subscriber (the F1 core property);
  - a tail attached mid-run replays from a cursor and receives live events;
  - abandoning a tail does not disturb the turn;
  - single-flight per session (busy → RuntimeError / 409 semantics);
  - abort cancels the task and records "aborted";
  - terminal errors carry the rate-limit classification;
  - retry_last re-runs the ORIGINAL message, only after an error;
  - the F3 dangling-handback auto-continue (fake runner, bounded);
  - extract_adk_web_server finds the instance behind get_fast_api_app.

Run: `uv run python tests/test_turn_broker.py`
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
_TMP = tempfile.mkdtemp(prefix="adk-cc-turnbroker-")
os.environ.setdefault("ADK_CC_DESKTOP", "1")
os.environ.setdefault("ADK_CC_DESKTOP_DATA", _TMP)
os.environ.setdefault("ADK_CC_WORKSPACE_ROOT", _TMP)

from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402
from pydantic import Field  # noqa: E402

from adk_cc.service.turns import (  # noqa: E402
    Turn,
    TurnBroker,
    _is_dangling_handback,
)


def _text_resp(t: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=t)]),
        partial=False,
    )


class _GatedLlm(BaseLlm):
    """Scripted model whose responses wait on per-call gates, so tests control
    exactly when the turn progresses."""

    model: str = "fake/turnbroker"
    responses: list = Field(default_factory=list)
    gates: list = Field(default_factory=list)
    calls: int = 0

    async def generate_content_async(self, req, stream: bool = False):
        i = self.calls
        self.calls += 1
        if i < len(self.gates):
            await self.gates[i].wait()
        yield self.responses.pop(0)


def _fresh(responses, gates=()):
    """A real ADK Runner over the REAL adk_cc App, model swapped for the
    scripted one. Fresh session service per case (isolation)."""
    import adk_cc.agent as A

    llm = _GatedLlm(responses=list(responses), gates=list(gates))
    A.MODEL._resolve_delegate = lambda: llm  # bypass registry entirely
    svc = InMemorySessionService()
    runner = Runner(app=A.app, session_service=svc,
                    artifact_service=None, memory_service=None)

    async def get_runner(app_name: str):
        return runner

    return TurnBroker(get_runner=get_runner, session_service=svc), svc, llm


def _msg(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


async def _mk_session(svc) -> str:
    import adk_cc.agent as A

    s = await svc.create_session(app_name=A.app.name, user_id="u1")
    return s.id


def test_turn_completes_without_subscriber() -> None:
    async def main():
        broker, svc, _ = _fresh([_text_resp("hello there")])
        sid = await _mk_session(svc)
        turn = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                            new_message=_msg("hi"))
        await asyncio.wait_for(turn.task, timeout=30)
        assert turn.status == "done", turn.snapshot()
        assert turn.model_events >= 1
        # events persisted server-side even though nobody watched
        s = await svc.get_session(app_name="adk_cc", user_id="u1", session_id=sid)
        assert any(getattr(e, "author", "") != "user" for e in s.events)
    asyncio.run(main())
    print("OK test_turn_completes_without_subscriber")


def test_tail_replays_and_survives_abandonment() -> None:
    async def main():
        gate = asyncio.Event()
        broker, svc, _ = _fresh([_text_resp("slow reply")], gates=[gate])
        sid = await _mk_session(svc)
        turn = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                            new_message=_msg("hi"))
        # attach a tail, read nothing yet, abandon it immediately (disconnect)
        t1 = turn.tail(0)
        await t1.aclose()
        assert turn.status == "running"
        gate.set()  # let the model answer
        await asyncio.wait_for(turn.task, timeout=30)
        assert turn.status == "done"
        # late re-attach replays EVERYTHING from cursor 0
        got = [p async for p in turn.tail(0) if p]
        assert len(got) == len(turn.events) and len(got) >= 1
        # cursor replay skips already-seen events
        got2 = [p async for p in turn.tail(len(turn.events)) if p]
        assert got2 == []
    asyncio.run(main())
    print("OK test_tail_replays_and_survives_abandonment")


def test_single_flight_and_abort() -> None:
    async def main():
        gate = asyncio.Event()
        broker, svc, _ = _fresh([_text_resp("never delivered")], gates=[gate])
        sid = await _mk_session(svc)
        turn = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                            new_message=_msg("hi"))
        try:
            broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                         new_message=_msg("again"))
            raise AssertionError("expected busy")
        except RuntimeError as e:
            assert "busy" in str(e)
        assert await broker.abort(turn.id) is True
        # poll on STATUS (a pre-start cancel resolves via the broker's
        # done-callback, one loop tick after the task completes)
        for _ in range(100):
            if turn.status != "running":
                break
            await asyncio.sleep(0.1)
        assert turn.status == "aborted", turn.snapshot()
        # an aborted turn is not retryable (retry is for errors only) …
        try:
            broker.retry_last(app_name="adk_cc", user_id="u1", session_id=sid)
            raise AssertionError("aborted turn must not be retryable")
        except LookupError:
            pass
        # … but the session itself is free for a fresh start
        gate.set()
        assert broker.latest_for("adk_cc", "u1", sid).status == "aborted"
    asyncio.run(main())
    print("OK test_single_flight_and_abort")


def test_error_classification_and_retry_last() -> None:
    class _RL(Exception):
        status_code = 429

    class _FailingLlm(BaseLlm):
        model: str = "fake/failing"
        fails_left: int = 1

        async def generate_content_async(self, req, stream: bool = False):
            if self.fails_left > 0:
                self.fails_left -= 1
                raise _RL("google/x:free is temporarily rate-limited upstream")
            yield _text_resp("recovered")

    async def main():
        import adk_cc.agent as A

        llm = _FailingLlm()
        A.MODEL._resolve_delegate = lambda: llm
        # zero in-model retries so the broker sees the failure immediately
        os.environ["ADK_CC_MODEL_RETRIES"] = "0"
        try:
            svc = InMemorySessionService()
            runner = Runner(app=A.app, session_service=svc,
                            artifact_service=None, memory_service=None)

            async def get_runner(app_name):
                return runner

            broker = TurnBroker(get_runner=get_runner, session_service=svc)
            sid = await _mk_session(svc)
            turn = broker.start(app_name="adk_cc", user_id="u1",
                                session_id=sid, new_message=_msg("hi"))
            await asyncio.wait_for(asyncio.gather(turn.task,
                                                  return_exceptions=True), 30)
            assert turn.status == "error", turn.snapshot()
            assert turn.error and turn.error["rate_limited"] is True
            assert turn.error["kind"] == "upstream", turn.error
            # retry re-runs the ORIGINAL message and succeeds
            turn2 = broker.retry_last(app_name="adk_cc", user_id="u1",
                                      session_id=sid)
            assert turn2.new_message is turn.new_message
            await asyncio.wait_for(turn2.task, timeout=30)
            assert turn2.status == "done", turn2.snapshot()
        finally:
            os.environ.pop("ADK_CC_MODEL_RETRIES", None)
    asyncio.run(main())
    print("OK test_error_classification_and_retry_last")


def test_dangling_handback_autocontinue() -> None:
    # Unit level: the predicate + the bounded continue loop over a fake runner.
    hb = types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(name="_handback_to_coordinator", args={}))])

    class _Ev:
        def __init__(self, content, author="Explore"):
            self.content = content
            self.author = author

        def model_dump_json(self, **kw):
            return "{}"

    assert _is_dangling_handback(_Ev(hb))
    assert not _is_dangling_handback(_Ev(types.Content(role="model",
        parts=[types.Part(text="done")])))

    class _FakeRunner:
        def __init__(self):
            self.rounds = 0
            self.messages = []
            self.deltas = []

        async def run_async(self, *, user_id, session_id, new_message,
                            state_delta=None):
            self.rounds += 1
            self.messages.append(new_message)
            self.deltas.append(state_delta)
            if self.rounds == 1:
                yield _Ev(hb)                     # resumed run dies on handback
            else:
                yield _Ev(types.Content(role="model",
                          parts=[types.Part(text="coordinator reply")]),
                          author="coordinator")

    async def main():
        fr = _FakeRunner()

        async def get_runner(app_name):
            return fr

        broker = TurnBroker(get_runner=get_runner, session_service=None)
        turn = broker.start(app_name="a", user_id="u", session_id="s",
                            new_message="answer-to-confirmation")
        await asyncio.wait_for(turn.task, timeout=10)
        assert turn.status == "done"
        assert fr.rounds == 2, fr.rounds          # exactly one auto-continue
        parts = getattr(fr.messages[1], "parts", None)
        assert parts and parts[0].text == "Continue."
        # The synthetic message is MARKED via the round's state_delta (a
        # temp: key rides the injected user event; the UI hides the bubble).
        assert fr.deltas[1] == {"temp:adk_cc_auto_continue": True}, fr.deltas
    asyncio.run(main())
    print("OK test_dangling_handback_autocontinue")


def test_error_event_ends_turn_no_autocontinue() -> None:
    """Measured live: Explore handed back, the coordinator's model call blew
    the context window — surfaced as an in-stream ERROR EVENT, not an
    exception — and the broker (a) auto-continued into the identical failure
    and (b) finished the turn "done", burying the error under a stray
    "Continue." bubble. An unresolved error event must end the turn AS an
    error, with no continue."""
    hb = types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(name="_handback_to_coordinator", args={}))])

    class _Ev:
        def __init__(self, content, author="Explore", error_code=None,
                     error_message=None):
            self.content = content
            self.author = author
            self.error_code = error_code
            self.error_message = error_message

        def model_dump_json(self, **kw):
            return "{}"

    class _FakeRunner:
        def __init__(self):
            self.rounds = 0

        async def run_async(self, *, user_id, session_id, new_message,
                            state_delta=None):
            self.rounds += 1
            yield _Ev(hb)
            yield _Ev(None, author="coordinator",
                      error_code="chatgpt_stream_error",
                      error_message="Your input exceeds the context window")

    async def main():
        fr = _FakeRunner()

        async def get_runner(app_name):
            return fr

        broker = TurnBroker(get_runner=get_runner, session_service=None)
        turn = broker.start(app_name="a", user_id="u", session_id="s",
                            new_message="survey question")
        await asyncio.wait_for(turn.task, timeout=10)
        assert fr.rounds == 1, f"auto-continued into the same failure: {fr.rounds}"
        assert turn.status == "error", turn.status
        assert turn.error and turn.error["type"] == "chatgpt_stream_error", turn.error
        assert "context window" in turn.error["message"], turn.error

    asyncio.run(main())
    print("OK test_error_event_ends_turn_no_autocontinue")


def test_error_event_recovered_by_later_text_still_completes() -> None:
    """A transient in-stream error the model speaks past must NOT fail the
    turn — only an error nothing recovered from is terminal."""
    class _Ev:
        def __init__(self, content, author="coordinator", error_code=None):
            self.content = content
            self.author = author
            self.error_code = error_code

        def model_dump_json(self, **kw):
            return "{}"

    class _FakeRunner:
        async def run_async(self, *, user_id, session_id, new_message,
                            state_delta=None):
            yield _Ev(None, error_code="transient_hiccup")
            yield _Ev(types.Content(role="model",
                      parts=[types.Part(text="recovered, here is the answer")]))

    async def main():
        async def get_runner(app_name):
            return _FakeRunner()

        broker = TurnBroker(get_runner=get_runner, session_service=None)
        turn = broker.start(app_name="a", user_id="u", session_id="s",
                            new_message="q")
        await asyncio.wait_for(turn.task, timeout=10)
        assert turn.status == "done", (turn.status, turn.error)

    asyncio.run(main())
    print("OK test_error_event_recovered_by_later_text_still_completes")


def test_snapshot_names_the_running_tool() -> None:
    """#105: a 15-95 minute turn of legitimate work (browser automation,
    model generation) was indistinguishable from a hang — the UI said only
    "agent is working…". The broker sees every event, so it can name what is
    in flight and how long the turn has run."""
    async def main():
        turn = Turn(app_name="a", user_id="u", session_id="s", new_message=None)

        call = json.dumps({"content": {"parts": [
            {"functionCall": {"id": "c1", "name": "run_bash", "args": {}}}]}})
        await turn.push(call, model_authored=True)
        snap = turn.snapshot()
        assert snap["current_tool"] == "run_bash", snap
        assert snap["elapsed_s"] >= 0 and "current_tool_elapsed_s" in snap, snap

        # A second call in flight keeps a tool named after the first returns.
        await turn.push(json.dumps({"content": {"parts": [
            {"functionCall": {"id": "c2", "name": "web_fetch", "args": {}}}]}}),
            model_authored=True)
        await turn.push(json.dumps({"content": {"parts": [
            {"functionResponse": {"id": "c1", "name": "run_bash",
                                  "response": {}}}]}}), model_authored=False)
        assert turn.snapshot()["current_tool"] == "web_fetch", turn.snapshot()

        # All responses in → back to model generation, no tool named.
        await turn.push(json.dumps({"content": {"parts": [
            {"functionResponse": {"id": "c2", "name": "web_fetch",
                                  "response": {}}}]}}), model_authored=False)
        assert "current_tool" not in turn.snapshot(), turn.snapshot()

        # Malformed payloads must never break a turn — the label is cosmetic.
        await turn.push("not json at all {functionCall", model_authored=True)
        assert turn.snapshot()["status"] == "running"

        # A finished turn carries no live progress fields.
        await turn.finish("done")
        assert "elapsed_s" not in turn.snapshot()

    asyncio.run(main())
    print("OK test_snapshot_names_the_running_tool")


def test_extract_adk_web_server() -> None:
    from adk_cc.service.turns import extract_adk_web_server

    os.environ["ADK_CC_ALLOW_NO_AUTH"] = "1"
    from adk_cc.service.server import make_app

    app = make_app()
    server = extract_adk_web_server(app)
    assert server is not None, "AdkWebServer not found — extraction broke"
    assert hasattr(server, "session_service") and hasattr(server, "get_runner_async")
    # and the broker routes actually mounted
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/turns" in paths and "/api/turns/{turn_id}/stream" in paths, paths
    print("OK test_extract_adk_web_server")


def main() -> None:
    test_turn_completes_without_subscriber()
    test_tail_replays_and_survives_abandonment()
    test_single_flight_and_abort()
    test_error_classification_and_retry_last()
    test_dangling_handback_autocontinue()
    test_error_event_ends_turn_no_autocontinue()
    test_error_event_recovered_by_later_text_still_completes()
    test_snapshot_names_the_running_tool()
    test_extract_adk_web_server()
    print("\nall turn-broker tests passed")


if __name__ == "__main__":
    main()
