"""When an empty round means "reply to me" and when it means "still waiting".

Answering `ask_user_question` halted the agent (desktop dogfooding, reproduced:
0 tool calls and 0 messages after the answer). ADK appends the functionResponse
and the resumed invocation ends WITHOUT yielding a single event, so the broker's
`not saw_any` short-circuit finished the turn silently.

The fix continues such a round — which makes the opposite mistake possible and
worse: auto-continuing a run that is parked on an UNANSWERED long-running call
would answer a permission prompt or a plan approval on the user's behalf. With
no events to inspect, that judgement comes from the session, and this is where
it is pinned.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_turn_resume_after_answer.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.service.turns import Turn, TurnBroker  # noqa: E402


class _FR:
    def __init__(self, id_, name="ask_user_question"):
        self.id, self.name, self.response = id_, name, {}


class _FC:
    def __init__(self, name):
        self.name, self.args = name, {}


class _Part:
    def __init__(self, *, text=None, call=None, resp=None):
        self.text, self.function_call, self.function_response = text, call, resp
        self.thought = False


class _Content:
    def __init__(self, parts):
        self.parts = parts


class _Ev:
    def __init__(self, author, parts, long_running=None):
        self.author = author
        self.content = _Content(parts)
        self.long_running_tool_ids = long_running
        self.partial = False


class _Session:
    def __init__(self, events):
        self.events = events


class _Svc:
    def __init__(self, session):
        self._s = session

    async def get_session(self, **_kw):
        return self._s


def _ask(call_id):
    return _Ev("coordinator", [_Part(call=_FC("ask_user_question"))],
               long_running=[call_id])


def _answer(call_id):
    return _Ev("user", [_Part(resp=_FR(call_id))])


def _reply(text="Got it."):
    return _Ev("coordinator", [_Part(text=text)])


def _needs_reply(events) -> bool:
    broker = TurnBroker(get_runner=None, session_service=_Svc(_Session(events)))
    turn = Turn(app_name="adk_cc", user_id="p1", session_id="s1", new_message=None)
    return asyncio.run(broker._answer_needs_reply(turn))


def test_the_answered_question_wants_a_reply() -> None:
    """The reported bug, as state: asked, answered, nothing said since."""
    assert _needs_reply([_ask("c1"), _answer("c1")]) is True
    print("OK the_answered_question_wants_a_reply")


def test_an_unanswered_prompt_is_left_alone() -> None:
    """THE case that must not regress. A pending confirmation, plan approval or
    question is parked deliberately — continuing would answer for the user."""
    assert _needs_reply([_ask("c1")]) is False
    # And a second question asked after an earlier one was answered still parks.
    assert _needs_reply([_ask("c1"), _answer("c1"), _reply(), _ask("c2")]) is False
    print("OK an_unanswered_prompt_is_left_alone")


def test_an_answer_that_already_got_a_reply_is_done() -> None:
    """Otherwise every later empty round would re-nudge the same answer."""
    assert _needs_reply([_ask("c1"), _answer("c1"), _reply()]) is False
    print("OK an_answer_that_already_got_a_reply_is_done")


def test_a_session_with_no_long_running_call_is_untouched() -> None:
    """Ordinary conversation must not acquire a phantom continue."""
    assert _needs_reply([_reply("hello")]) is False
    assert _needs_reply([]) is False
    print("OK a_session_with_no_long_running_call_is_untouched")


def test_a_missing_or_unreadable_session_does_not_continue() -> None:
    """Fail closed: a probe that cannot answer must not invent a nudge."""

    class _Broken:
        async def get_session(self, **_kw):
            raise RuntimeError("store down")

    class _None:
        async def get_session(self, **_kw):
            return None

    turn = Turn(app_name="adk_cc", user_id="p1", session_id="s1", new_message=None)
    for svc in (_Broken(), _None()):
        broker = TurnBroker(get_runner=None, session_service=svc)
        assert asyncio.run(broker._answer_needs_reply(turn)) is False
    print("OK a_missing_or_unreadable_session_does_not_continue")


def test_several_answers_in_one_batch() -> None:
    """A form can answer more than one call at once; all of them being closed
    still means the user is owed a reply."""
    ev = _Ev("user", [_Part(resp=_FR("c1")), _Part(resp=_FR("c2"))])
    assert _needs_reply([_ask("c1"), _ask("c2"), ev]) is True
    assert _needs_reply([_ask("c1"), _ask("c2"), _answer("c1")]) is False  # c2 open
    print("OK several_answers_in_one_batch")


def main() -> None:
    test_the_answered_question_wants_a_reply()
    test_an_unanswered_prompt_is_left_alone()
    test_an_answer_that_already_got_a_reply_is_done()
    test_a_session_with_no_long_running_call_is_untouched()
    test_a_missing_or_unreadable_session_does_not_continue()
    test_several_answers_in_one_batch()
    print("\nall turn-resume tests passed")


if __name__ == "__main__":
    main()
