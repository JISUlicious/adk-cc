"""Answering one of several stacked confirmations must not strand the rest.

Reported live, twice. A model raised TWO gated `run_skill_script` calls in one
round, so two cards appeared. Clicking the first greyed out the second — it
became unclickable and the run was stuck. The session showed exactly:

    FC  run_skill_script            x2
    FR  needs confirmation          x2
    FC  adk_cc_confirmation_form    x2
    FR  adk_cc_pending_confirmation x1   (new invocation id)
    coordinator: no parts, and it stopped there.

Cause, reproduced against the broker's own predicates: two helpers disagreed
about the same message. `_is_confirmation_answer` matches the plugin's names
INCLUDING the stash, so the answer was routed into a fresh invocation;
`_answered_ids` deliberately EXCLUDES that stash, because ADK never received
it, so the broker simultaneously believed nothing had been answered. The model
was then handed two function calls and zero responses — no legal move — and
emitted an event with no parts. The turn ended, the surviving card's
invocation went with it, and the UI greyed it out.

The rule this pins: a model turn is only legal when every outstanding call has
a response, so a PARTIAL batch must park rather than run. Parking ENDS the
turn on purpose — that keeps the remaining cards live and their next click
starts one clean turn with the complete set. Holding the turn open would
re-open the single-flight window that produced spurious 409s.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_stacked_confirmations.py
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

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


# --- the shapes the broker reads (it only ever uses duck-typed access) ------
class _P:
    def __init__(self, fc=None, fr=None):
        self.function_call, self.function_response = fc, fr
        self.text = None


class _C:
    def __init__(self, parts):
        self.parts = parts


class _Call:
    def __init__(self, i, n):
        self.id, self.name = i, n


class _Resp:
    def __init__(self, i, n):
        self.id, self.name = i, n


class _E:
    def __init__(self, author, parts, lr=()):
        self.author, self.content = author, _C(parts)
        self.long_running_tool_ids = set(lr)
        self.partial = False


class _Session:
    def __init__(self, events):
        self.events = events


class _Svc:
    """Session service stub that records what the broker appends."""

    def __init__(self, events):
        self._events = events
        self.appended = []

    async def get_session(self, **kw):
        return _Session(list(self._events))

    async def append_event(self, session=None, event=None):
        self.appended.append(event)
        self._events.append(event)
        return event


def _reported_history():
    """The session exactly as reported: two gated calls, two cards, none clicked."""
    return [
        _E("coordinator",
           [_P(fc=_Call("a", "run_skill_script")), _P(fc=_Call("b", "run_skill_script"))],
           lr=("a", "b")),
        _E("coordinator",
           [_P(fr=_Resp("a", "run_skill_script")), _P(fr=_Resp("b", "run_skill_script"))]),
        _E("coordinator",
           [_P(fc=_Call("fa", "adk_cc_confirmation_form")),
            _P(fc=_Call("fb", "adk_cc_confirmation_form"))],
           lr=("fa", "fb")),
    ]


def main() -> int:  # noqa: PLR0915
    from adk_cc.service.turns import (Turn, TurnBroker, _is_confirmation_answer,
                                      _user_answered_ids)

    # 1. The two predicates must no longer contradict each other.
    answer = _C([_P(fr=_Resp("fa", "adk_cc_pending_confirmation"))])
    check("a stash answer still routes to a new invocation",
          _is_confirmation_answer(answer))
    check("and it now COUNTS as user-answered",
          _user_answered_ids(type("_M", (), {"content": answer})()) == {"fa"},
          "the click happened whatever the delivery layer did with it")

    # 2. Answering ONE of two must park, not run.
    svc = _Svc(_reported_history())
    broker = TurnBroker.__new__(TurnBroker)
    broker.session_service = svc

    turn = Turn.__new__(Turn)
    turn.id, turn.app_name, turn.user_id, turn.session_id = "t1", "adk_cc", "u", "s"
    turn.new_message = answer

    outstanding = asyncio.run(broker._outstanding_confirmations(turn))
    check("one answer leaves the batch incomplete", outstanding == {"fb"},
          f"outstanding={sorted(outstanding)}")

    # 3. The second answer completes it — and only then may the model run.
    svc2 = _Svc(_reported_history()
                + [_E("user", [_P(fr=_Resp("fa", "adk_cc_pending_confirmation"))])])
    broker2 = TurnBroker.__new__(TurnBroker)
    broker2.session_service = svc2
    turn2 = Turn.__new__(Turn)
    turn2.id, turn2.app_name, turn2.user_id, turn2.session_id = "t2", "adk_cc", "u", "s"
    turn2.new_message = _C([_P(fr=_Resp("fb", "adk_cc_pending_confirmation"))])

    outstanding2 = asyncio.run(broker2._outstanding_confirmations(turn2))
    check("the last answer completes the batch", outstanding2 == set(),
          f"outstanding={sorted(outstanding2)}")

    # 4. A parked answer is BUFFERED, never written to the session. Writing
    #    it is what kept the batch broken: it carries the plugin's stash name,
    #    which ADK ignores, so replaying history left the first call
    #    unanswered and clicking BOTH cards stalled exactly like clicking one.
    buf = broker._park_buffer(turn)
    buf.extend(answer.parts)
    check("the parked answer is NOT written to the session",
          svc.appended == [], f"appended={svc.appended}")
    check("it is held in the buffer instead",
          _user_answered_ids(type("_M", (), {"content": _C(buf)})()) == {"fa"})

    # 5. The buffer is what completes the batch on the final click.
    turn3 = Turn.__new__(Turn)
    turn3.id, turn3.app_name, turn3.user_id, turn3.session_id = "t3", "adk_cc", "u", "s"
    turn3.new_message = _C([_P(fr=_Resp("fb", "adk_cc_pending_confirmation"))])
    check("buffered + incoming completes the batch",
          asyncio.run(broker._outstanding_confirmations(turn3, buf)) == set())
    check("and WITHOUT the buffer it would not",
          asyncio.run(broker._outstanding_confirmations(turn3)) == {"fa"},
          "the buffer must be what closes the batch")

    # 6. The ordinary single-confirmation path must be untouched: one card,
    #    one answer, nothing outstanding, so it runs immediately.
    solo = _Svc([
        _E("coordinator", [_P(fc=_Call("x", "run_bash"))], lr=("x",)),
        _E("coordinator", [_P(fr=_Resp("x", "run_bash"))]),
        _E("coordinator", [_P(fc=_Call("fx", "adk_cc_confirmation_form"))], lr=("fx",)),
    ])
    b3 = TurnBroker.__new__(TurnBroker)
    b3.session_service = solo
    t4 = Turn.__new__(Turn)
    t4.id, t4.app_name, t4.user_id, t4.session_id = "t4", "adk_cc", "u", "s"
    t4.new_message = _C([_P(fr=_Resp("fx", "adk_cc_pending_confirmation"))])
    check("a single confirmation never parks (no regression)",
          asyncio.run(b3._outstanding_confirmations(t4)) == set())

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
