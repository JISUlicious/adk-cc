"""A behaviour claim about a page built in an EARLIER turn gets labelled.

Measured across three live runs of the same four-turn scenario: turn 2 builds
`index.html`, turn 3 asks "does the colour control work?". In two of the runs
the agent answered "Yes …" from memory — zero tool calls — and in one of them
the claim was FALSE: the page's Three.js never loads (`Failed to resolve module
specifier "three"`), yet the agent confirmed drag-to-rotate and live colour
switching. The turn-scoped signal is structurally blind here (`built_a_page`
is about THIS turn), and the request-side nudge cannot fire either — at
before_model the claim does not exist yet.

So the check moved to where the claim does exist (`after_model`), across the
whole session's events, and in soft mode the answer is LABELLED: one visible
line saying the page has not been driven since it changed. A true-but-lucky
claim and a shipped falsehood look identical to the user otherwise — that is
the exact lesson of runs 2 and 3.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_stale_page_claim.py
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

from google.genai import types  # noqa: E402

from adk_cc.plugins.verify_nudge import VerifyNudgePlugin  # noqa: E402
from adk_cc.verification.signals import undriven_pages  # noqa: E402

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


# ---- event fakes (same shape the signal tests use) -------------------------
class _FC:
    def __init__(self, name, args):
        self.name, self.args = name, args


class _Part:
    def __init__(self, *, call=None, text=None):
        self.function_call, self.function_response, self.text = call, None, text
        self.thought = False


class _Content:
    def __init__(self, parts):
        self.parts = parts


class _Ev:
    def __init__(self, parts, author="coordinator", inv="t1"):
        self.content, self.author, self.partial = _Content(parts), author, False
        self.long_running_tool_ids = None
        self.invocation_id = inv


def _build_turn(inv="t1", path="index.html"):
    return [
        _Ev([_Part(call=_FC("write_file", {"path": path, "content": "<html/>"}))], inv=inv),
        _Ev([_Part(text="Done — the page is in place.")], inv=inv),
    ]


def _drive_turn(inv="t2"):
    return [_Ev([_Part(call=_FC("run_skill_script", {
        "skill_name": "web-smoke-check", "file_path": "scripts/smoke_page.py",
        "args": ["index.html", "check.mjs"]}))], inv=inv)]


# ---- undriven_pages itself --------------------------------------------------
def test_tracker() -> None:
    ev = _build_turn()
    check("a written page starts undriven", undriven_pages(ev) == ("index.html",),
          undriven_pages(ev))
    ev2 = ev + _drive_turn()
    check("driving it clears the flag", undriven_pages(ev2) == (), undriven_pages(ev2))
    ev3 = ev2 + _build_turn(inv="t3")
    check("an edit AFTER the drive makes it stale again",
          undriven_pages(ev3) == ("index.html",), undriven_pages(ev3))
    ev4 = ev + [_Ev([_Part(call=_FC("run_bash",
                                    {"command": "npx playwright test"}))], inv="t2")]
    check("a browser-runner command counts as a drive too",
          undriven_pages(ev4) == (), undriven_pages(ev4))


# ---- the plugin, across a turn boundary ------------------------------------
class _Session:
    def __init__(self, events):
        self.events = events


class _ICtx:
    def __init__(self, events, agent="coordinator"):
        self.session = _Session(events)
        self.agent = type("A", (), {"name": agent})()


class _Cb:
    def __init__(self, events, inv):
        self._invocation_context = _ICtx(events)
        self.invocation_id = inv
        self.state = {}


def _answer(text):
    from google.adk.models.llm_response import LlmResponse

    return LlmResponse(content=types.Content(
        role="model", parts=[types.Part(text=text)]))


def _run(events, inv, answer):
    plugin = VerifyNudgePlugin()
    return asyncio.run(plugin.after_model_callback(
        callback_context=_Cb(events, inv), llm_response=_answer(answer)))


def test_labelling() -> None:
    # Turn 3 of the live scenario: page built in t1, claim in t3, no tools.
    events = _build_turn(inv="t1") + [
        _Ev([_Part(text="Confirm the colour control works")], author="user", inv="t3")]

    out = _run(events, "t3",
               "Yes — the colour control changes the live preview on the page. "
               "Clicking a swatch updates the lamp shade colour. Done.")
    text = "".join(p.text or "" for p in out.content.parts) if out else ""
    check("the run-2/run-3 shape gets a visible label", out is not None, "(no alteration)")
    check("the label names the page and says 'not verified'",
          "index.html" in text and "not verified" in text.lower()
          or "asserted, not verified" in text, text[-160:])
    check("the original answer is preserved above the label",
          "colour control changes" in text, text[:120])

    # The same claim AFTER a drive: no label.
    driven = _build_turn(inv="t1") + _drive_turn(inv="t2") + [
        _Ev([_Part(text="Confirm it works")], author="user", inv="t3")]
    check("a claim about a DRIVEN page is left alone",
          _run(driven, "t3", "Yes — clicking the button updates the preview.") is None)

    # An unrelated claim in the same session: no label.
    check("an unrelated 'Done' is not second-guessed",
          _run(events, "t3", "Done — renamed the config key as asked.") is None)

    # An honest hedge is the other acceptable outcome.
    check("an honest hedge is not labelled twice",
          _run(events, "t3",
               "The control should work, but I have not verified it in a "
               "browser — the page has not been driven.") is None)

    # Evidence THIS turn silences it.
    evidence = events + [_Ev([_Part(call=_FC("run_skill_script", {
        "skill_name": "web-smoke-check", "file_path": "scripts/smoke_page.py",
        "args": ["index.html", "check.mjs"]}))], inv="t3")]
    check("driving the page in the claiming turn silences it",
          _run(evidence, "t3", "Yes — verified: clicking updates the page.") is None)


def main() -> int:
    test_tracker()
    test_labelling()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
