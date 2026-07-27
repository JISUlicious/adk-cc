"""W9 S1/S2: turn-level verification signals, contracts, and the soft nudge.

The failure this targets is real and measured: a dogfooding round produced three
"fixed" claims that a live reproduction contradicted — none involving a skill.
So the unit of verification is the CLAIM, and these detectors must fire on
"asserted a result, checked nothing" while staying quiet everywhere else.

A noisy nudge is worse than none (users learn to ignore it), so the quiet cases
get as much coverage as the firing ones.

Run: `uv run python tests/test_verification_signals.py`
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.verification.contract import VERIFY_METADATA_KEY, parse  # noqa: E402
from adk_cc.verification.signals import collect, nudge_text  # noqa: E402


def _text(t, author="coordinator", thought=False):
    return SimpleNamespace(
        author=author, invocation_id="inv",
        content=SimpleNamespace(parts=[SimpleNamespace(
            text=t, thought=thought, function_call=None, function_response=None)]),
    )


def _call(name, author="coordinator", **args):
    fc = SimpleNamespace(name=name, args=args or {})
    return SimpleNamespace(
        author=author, invocation_id="inv",
        content=SimpleNamespace(parts=[SimpleNamespace(
            text=None, thought=False, function_call=fc, function_response=None)]),
    )


# --- the primary trigger ---------------------------------------------------

def test_claim_without_evidence_fires():
    events = [
        _call("edit_file", path="a.py"),
        _text("Fixed the portal bug — it works now."),
    ]
    sig = collect(events, author="coordinator")
    assert sig.claims and not sig.has_evidence
    assert sig.claim_without_evidence, sig.summary()
    msg = nudge_text(sig)
    assert msg and "executed a check" in msg
    print("OK claim_without_evidence_fires")


def test_claim_with_evidence_is_quiet():
    """Ran the tests, then claimed. Exactly the behaviour we want — silence."""
    events = [
        _call("edit_file", path="a.py"),
        _call("run_bash", command="python -m pytest -q tests/"),
        _text("Fixed it — tests pass."),
    ]
    sig = collect(events, author="coordinator")
    assert sig.has_evidence and not sig.claim_without_evidence, sig.summary()
    assert nudge_text(sig) is None
    print("OK claim_with_evidence_is_quiet")


def test_hedged_claim_is_quiet():
    """Saying 'unverified' is the desired outcome, not a violation."""
    events = [
        _call("edit_file", path="a.py"),
        _text("I changed the handler, but this is unverified — I could not run "
              "the suite here."),
    ]
    sig = collect(events, author="coordinator")
    assert sig.hedged and not sig.claim_without_evidence, sig.summary()
    assert nudge_text(sig) is None
    print("OK hedged_claim_is_quiet")


def test_no_claim_is_quiet():
    """Plain reporting must not trip the detector."""
    for t in ("I read the config and it uses Postgres.",
              "Here are three options for the schema.",
              "I ran the tests and 3 failed; here is the output."):
        sig = collect([_call("read_file", path="x"), _text(t)], author="coordinator")
        assert not sig.claim_without_evidence, (t, sig.summary())
        assert nudge_text(sig) is None, t
    print("OK no_claim_is_quiet")


def test_subagent_chatter_is_not_a_user_claim():
    """A specialist saying 'fixed' internally is not the coordinator's claim."""
    events = [_call("edit_file", path="a.py"),
              _text("Fixed it.", author="verification")]
    sig = collect(events, author="coordinator")
    assert not sig.claims, sig.summary()
    print("OK subagent_chatter_is_not_a_user_claim")


# --- secondary triggers ----------------------------------------------------

def test_live_gaps_bare_done_and_inline_probe():
    """Both found by a live turn: the model answered with a bare "Done." after
    an edit (claim missed), and verified via `python - <<PY` (evidence missed).
    Missing evidence is the worse bug — it would nudge a turn that DID check."""
    # bare "Done." is a claim
    sig = collect([_call("edit_file", path="a.py"), _text("Done.")], author="coordinator")
    assert sig.claims == ("done",), sig.summary()
    assert sig.claim_without_evidence
    # ...but prose containing the word is not
    for t in ("when done, run the suite", "I am not done reading yet"):
        assert not collect([_text(t)], author="coordinator").claims, t
    # an inline interpreter probe IS evidence
    for cmd in ("python - <<'PY'\nimport calc\nprint(calc.add(2,3))\nPY",
                'python -c "import calc; print(calc.add(2,3))"', "node -e 'require(\"./a\")'"):
        assert collect([_call("run_bash", command=cmd)], author="coordinator").ran_checks == 1, cmd
    # the real live shape: edit + inline probe + "Done." -> quiet, correctly
    real = [_call("edit_file", path="calc.py"),
            _call("run_bash", command="python - <<'PY'\nimport calc\nprint(calc.add(2,3))\nPY"),
            _text("Done.")]
    sig = collect(real, author="coordinator")
    assert sig.claims and sig.has_evidence and not sig.claim_without_evidence, sig.summary()
    assert nudge_text(sig) is None
    print("OK live_gaps_bare_done_and_inline_probe")


def test_risk_class_detected():
    events = [_call("run_bash", command="git push --force origin main"),
              _text("Pushed.")]
    sig = collect(events, author="coordinator")
    assert sig.risky and "git push" in " ".join(sig.risk_hits), sig.summary()
    msg = nudge_text(sig)
    assert msg and "irreversible" in msg
    print("OK risk_class_detected")


def test_risk_with_evidence_is_quiet():
    events = [
        _call("run_bash", command="python -m pytest -q"),
        _call("run_bash", command="git push origin main"),
        _text("Pushed."),
    ]
    sig = collect(events, author="coordinator")
    assert sig.risky and sig.has_evidence
    assert nudge_text(sig) is None, "a checked risky turn should stay quiet"
    print("OK risk_with_evidence_is_quiet")


def test_check_commands_recognized():
    for cmd in ("pytest -q", "npm test", "go test ./...", "curl -s localhost:8000/health",
                "ruff check .", "python3 -m pytest tests/", "make check"):
        sig = collect([_call("run_bash", command=cmd)], author="coordinator")
        assert sig.ran_checks == 1, cmd
    for cmd in ("mkdir -p build", "echo hi", "git add -A"):
        sig = collect([_call("run_bash", command=cmd)], author="coordinator")
        assert sig.ran_checks == 0, cmd
    print("OK check_commands_recognized")


def test_empty_turn_is_quiet():
    assert nudge_text(collect([], author="coordinator")) is None
    assert nudge_text(collect([_text("Hello!")], author="coordinator")) is None
    print("OK empty_turn_is_quiet")


# --- contracts (S1) --------------------------------------------------------

def test_predictive_trigger_fires_before_the_claim_exists():
    """THE timing bug, found live: a turn wrote a file and answered "Done." with
    the nudge silent throughout — because at before_model time the claim has not
    been produced yet, so a claim-keyed nudge can never fire in time. The
    predictive signal ('changed something, checked nothing') is what IS knowable
    beforehand."""
    mid_turn = [_call("write_file", path="README.md")]   # no answer text yet
    sig = collect(mid_turn, author="coordinator")
    assert not sig.claims, "the claim does not exist yet — that is the point"
    assert sig.unchecked_change
    assert nudge_text(sig) is None, "reactive nudge correctly silent"
    msg = nudge_text(sig, predictive=True)
    assert msg and "changed the workspace" in msg
    # …and once a check has run, predictive goes quiet too
    checked = mid_turn + [_call("run_bash", command="pytest -q")]
    assert nudge_text(collect(checked, author="coordinator"), predictive=True) is None
    # read-only turns never fire, predictive or not
    ro = [_call("read_file", path="x"), _call("grep", pattern="y")]
    assert nudge_text(collect(ro, author="coordinator"), predictive=True) is None
    print("OK predictive_trigger_fires_before_the_claim_exists")


def test_contract_parsing():
    c = parse('{"mode":"verifier","checks":["cites evidence"],"commands":["pytest"]}', source="s")
    assert c.mode == "verifier" and c.wants_verifier and c.is_active
    assert c.checks == ("cites evidence",) and c.commands == ("pytest",)
    assert parse("self").mode == "self"
    assert parse(None).mode == "none" and not parse(None).is_active
    # malformed must degrade, never raise — a bad manifest can't break loading
    assert parse("{not json").mode == "none"
    assert parse("{}").mode == "self"
    assert parse('{"mode":"bogus"}').mode == "self"
    assert parse(12345).mode == "none"
    print("OK contract_parsing")


def test_criteria_appear_in_the_nudge():
    events = [_call("edit_file", path="a.py"), _text("Fixed.")]
    sig = collect(events, author="coordinator")
    msg = nudge_text(sig, criteria=["every finding cites a file:line"])
    assert "Acceptance criteria" in msg and "file:line" in msg
    print("OK criteria_appear_in_the_nudge")


def test_builtin_skill_contracts_parse():
    """Whatever the built-ins declare must parse — a typo in frontmatter would
    silently disable verification for that skill."""
    from adk_cc.tools.skills import discover_skills_with_sources
    from adk_cc.verification.contract import contract_for_skill

    os.environ.setdefault("ADK_CC_DISABLE_PROJECT_SKILLS", "1")
    n = 0
    for s, _ in discover_skills_with_sources():
        c = contract_for_skill(s)
        assert c.mode in ("none", "self", "verifier")
        if c.is_active:
            n += 1
            assert c.checks, f"{s.frontmatter.name}: active contract with no checks"
    print(f"OK builtin_skill_contracts_parse ({n} active)")


# --- S3: the hard gate -----------------------------------------------------

def _gate(answer, turn_events, mode="hard", agent="coordinator", gated=None):
    """Drive VerifyNudgePlugin.after_model_callback with a fake context."""
    import asyncio as _a
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types
    from adk_cc.plugins.verify_nudge import VerifyNudgePlugin

    os.environ["ADK_CC_VERIFY"] = mode
    try:
        ictx = SimpleNamespace(
            agent=SimpleNamespace(name=agent),
            session=SimpleNamespace(events=turn_events),
        )
        cc = SimpleNamespace(
            _invocation_context=ictx, invocation_id="inv",
            state=(gated if gated is not None else {}),
        )
        resp = LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text=answer)]))
        return _a.run(VerifyNudgePlugin().after_model_callback(
            callback_context=cc, llm_response=resp))
    finally:
        os.environ.pop("ADK_CC_VERIFY", None)


def _transfers_to_verification(out):
    if out is None:
        return False
    for p in (out.content.parts or []):
        fc = getattr(p, "function_call", None)
        if fc and fc.name == "transfer_to_agent" and fc.args.get("agent_name") == "verification":
            return True
    return False


def test_gate_blocks_unverified_claim():
    """The 4-of-5 case the measurement found: edited, checked nothing, claimed."""
    out = _gate("Done. Added multiply().", [_call("edit_file", path="calc.py")])
    assert _transfers_to_verification(out), out
    print("OK gate_blocks_unverified_claim")


def test_gate_emits_no_user_facing_text():
    """Observed live: a text part in the gate response is recorded as a
    coordinator message and becomes the answer the user reads — the gate's
    internal scaffolding leaked into the conversation. The gate must route,
    not speak."""
    out = _gate("Done. Added multiply().", [_call("edit_file", path="calc.py")])
    assert out is not None
    texts = [p.text for p in (out.content.parts or []) if getattr(p, "text", None)]
    assert texts == [], f"gate must not emit user-facing text: {texts}"
    assert _transfers_to_verification(out)
    print("OK gate_emits_no_user_facing_text")


def test_gate_is_quiet_when_verified():
    turn = [_call("edit_file", path="calc.py"),
            _call("run_bash", command="python -m pytest -q")]
    assert _gate("Done — tests pass.", turn) is None
    print("OK gate_is_quiet_when_verified")


def test_gate_is_quiet_without_a_claim_or_change():
    # no claim
    assert _gate("Here is what the file contains.", [_call("edit_file", path="a")]) is None
    # no change
    assert _gate("Done.", [_call("read_file", path="a")]) is None
    # read-only turn entirely
    assert _gate("Done.", []) is None
    print("OK gate_is_quiet_without_a_claim_or_change")


def test_gate_respects_mode_and_fires_once():
    turn = [_call("edit_file", path="calc.py")]
    assert _gate("Done.", turn, mode="soft") is None, "soft must never block"
    assert _gate("Done.", turn, mode="off") is None
    # bounded: a turn already gated is not gated again (no ping-pong on FAIL)
    st = {"temp:verify_gated_invocation": "inv"}
    assert _gate("Done.", turn, gated=st) is None
    print("OK gate_respects_mode_and_fires_once")


def test_gate_never_blocks_the_verifier_itself():
    turn = [_call("edit_file", path="a", author="verification")]
    assert _gate("Done.", turn, agent="verification") is None
    print("OK gate_never_blocks_the_verifier_itself")


def test_gate_ignores_tool_calls_and_partials():
    """Only a final text answer is gated; a tool call is the turn continuing."""
    import asyncio as _a
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types
    from adk_cc.plugins.verify_nudge import VerifyNudgePlugin

    os.environ["ADK_CC_VERIFY"] = "hard"
    try:
        ictx = SimpleNamespace(agent=SimpleNamespace(name="coordinator"),
                               session=SimpleNamespace(events=[_call("edit_file", path="a")]))
        cc = SimpleNamespace(_invocation_context=ictx, invocation_id="inv", state={})
        tool_resp = LlmResponse(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name="read_file", args={}))]))
        assert _a.run(VerifyNudgePlugin().after_model_callback(
            callback_context=cc, llm_response=tool_resp)) is None
        partial = LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text="Done.")]), partial=True)
        assert _a.run(VerifyNudgePlugin().after_model_callback(
            callback_context=cc, llm_response=partial)) is None
    finally:
        os.environ.pop("ADK_CC_VERIFY", None)
    print("OK gate_ignores_tool_calls_and_partials")


def main():
    test_claim_without_evidence_fires()
    test_claim_with_evidence_is_quiet()
    test_hedged_claim_is_quiet()
    test_no_claim_is_quiet()
    test_subagent_chatter_is_not_a_user_claim()
    test_live_gaps_bare_done_and_inline_probe()
    test_risk_class_detected()
    test_risk_with_evidence_is_quiet()
    test_check_commands_recognized()
    test_empty_turn_is_quiet()
    test_predictive_trigger_fires_before_the_claim_exists()
    test_contract_parsing()
    test_criteria_appear_in_the_nudge()
    test_builtin_skill_contracts_parse()
    test_gate_blocks_unverified_claim()
    test_gate_emits_no_user_facing_text()
    test_gate_is_quiet_when_verified()
    test_gate_is_quiet_without_a_claim_or_change()
    test_gate_respects_mode_and_fires_once()
    test_gate_never_blocks_the_verifier_itself()
    test_gate_ignores_tool_calls_and_partials()
    print("\nall verification-signal tests passed")


if __name__ == "__main__":
    main()
