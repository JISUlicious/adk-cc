"""A behaviour claim about a page that was never loaded (W9 S4).

Taken from a real failure. A live run built a browser social-deduction game and
verified it with a syntax check, a grep proving the control ids were in the
HTML, and a scratch probe of the start flow. Every check passed, the answer said
VERDICT: PASS, and the shipped game erased the vote result in the same tick it
wrote it — so the payoff moment of the whole genre was invisible to players.

The old signals could not see this. Evidence was a scalar: `ran_checks > 0` made
`has_evidence` true, so the nudge stayed silent no matter WHAT was checked. A
probe of one flow vouched for claims about every other flow.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_unexercised_page.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.verification.signals import collect, nudge_text  # noqa: E402


class _FC:
    def __init__(self, name, args):
        self.name, self.args = name, args


class _Part:
    def __init__(self, *, call=None, text=None):
        self.function_call, self.text, self.thought = call, text, False


class _Content:
    def __init__(self, parts):
        self.parts = parts


class _Ev:
    def __init__(self, parts, author="coordinator"):
        self.content, self.author = _Content(parts), author


def _write(path):
    return _Part(call=_FC("write_file", {"path": path, "content": "…"}))


def _bash(command):
    return _Part(call=_FC("run_bash", {"command": command}))


def _turn(*parts):
    return [_Ev(list(parts))]


def test_the_shipped_failure_is_now_caught() -> None:
    """The exact shape of the run that shipped the bug."""
    sig = collect(_turn(
        _write("index.html"), _write("app.js"), _write("styles.css"),
        _bash("node --check app.js"),
        _bash("grep -R -n -E 'player-input|start-game|resolve-vote' index.html"),
        _bash("node -e \"const s={}; /* start flow probe */ console.log('ok')\""),
        _Part(text="Built it and verified. The voting flow works."),
    ))
    assert sig.has_evidence, "a probe did run — that was always true"
    assert sig.built_a_page and not sig.page_was_driven
    assert sig.unexercised_page, sig.summary()
    text = nudge_text(sig)
    assert text and "no check loaded it" in text, text
    assert "jsdom" in text, "the nudge must name a way to actually do it"
    print("OK the_shipped_failure_is_now_caught")


def test_driving_the_real_page_satisfies_it() -> None:
    """A DOM runtime is what the nudge asks for, so it must clear the signal."""
    for runner in (
        "node -e \"const {JSDOM}=require('jsdom'); …\"",
        "python3 -c \"from playwright.sync_api import sync_playwright; …\"",
        "npx playwright test",
    ):
        sig = collect(_turn(
            _write("index.html"), _write("app.js"),
            _bash(runner),
            _Part(text="Built it and verified. Voting resolves correctly."),
        ))
        assert not sig.unexercised_page, runner
    print("OK driving_the_real_page_satisfies_it")


def test_a_test_runner_counts_without_naming_the_file() -> None:
    """A suite covers files it never mentions — demanding the filename would
    make the signal fire on well-tested projects, which is worse than a miss."""
    sig = collect(_turn(
        _write("index.html"), _write("src/game.js"),
        _bash("npm test"),
        _Part(text="Done — implemented and passing."),
    ))
    assert not sig.unexercised_page
    print("OK a_test_runner_counts_without_naming_the_file")


def test_naming_the_file_is_not_running_it() -> None:
    """THE distinction. Both of these name index.html and neither loads it."""
    for cmd in ("node --check app.js",
                "grep -n 'id=' index.html",
                "cat index.html",
                "npx tsc --noEmit"):
        sig = collect(_turn(
            _write("index.html"), _bash(cmd),
            _Part(text="Fixed — the buttons work now."),
        ))
        assert sig.unexercised_page, cmd
    print("OK naming_the_file_is_not_running_it")


def test_hedging_still_defuses_it() -> None:
    """Saying it is unverified is the behaviour we want, not a violation."""
    sig = collect(_turn(
        _write("index.html"), _bash("node --check app.js"),
        _Part(text="Built the page. I could not verify the click flow — "
                   "there is no browser here, so it is untested."),
    ))
    assert not sig.unexercised_page
    print("OK hedging_still_defuses_it")


def test_non_page_work_is_untouched() -> None:
    """Narrowness check: this must not fire on ordinary backend turns, or it
    becomes noise on every project that has no HTML in it."""
    sig = collect(_turn(
        _write("service.py"), _bash("pytest -q"),
        _Part(text="Fixed and tests pass."),
    ))
    assert not sig.built_a_page and not sig.unexercised_page
    sig2 = collect(_turn(
        _write("README.md"),
        _Part(text="Updated the docs."),
    ))
    assert not sig2.unexercised_page
    print("OK non_page_work_is_untouched")


def test_no_claim_no_nudge() -> None:
    """Building a page without asserting anything about it is not a violation —
    the signal is about unbacked CLAIMS, not about page authoring."""
    sig = collect(_turn(
        _write("index.html"),
        _Part(text="Here is a first pass at the layout; nothing is wired yet."),
    ))
    assert not sig.unexercised_page
    print("OK no_claim_no_nudge")


def main() -> None:
    test_the_shipped_failure_is_now_caught()
    test_driving_the_real_page_satisfies_it()
    test_a_test_runner_counts_without_naming_the_file()
    test_naming_the_file_is_not_running_it()
    test_hedging_still_defuses_it()
    test_non_page_work_is_untouched()
    test_no_claim_no_nudge()
    print("\nall unexercised-page tests passed")


if __name__ == "__main__":
    main()
