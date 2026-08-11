"""#128 P0: guard calibration + pressure ladder.

Replays the incident shape at unit scale: an estimate that undercounts
(chars/4 vs the API's real prompt_token_count) and mid-turn growth that
sails between WARN and REJECT. Calibration must feed the decision; the
pressure line must rewrite aggressively BEFORE the reject line.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_context_pressure.py
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
os.environ.setdefault("ADK_CC_API_KEY", "stub")
for _k in list(os.environ):
    if _k.startswith(("ADK_CC_MAX_CONTEXT", "ADK_CC_CONTEXT_")):
        os.environ.pop(_k)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _plugin(max_tokens=2000, pressure_pct=None):
    os.environ["ADK_CC_MAX_CONTEXT_TOKENS"] = str(max_tokens)
    if pressure_pct is not None:
        os.environ["ADK_CC_CONTEXT_PRESSURE_PCT"] = str(pressure_pct)
    from adk_cc.plugins.context_guard import ContextGuardPlugin

    p = ContextGuardPlugin()
    p._session_events = lambda cb: []
    p._session_id = lambda cb: "s1"
    return p


def _request(payload_chars: int):
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    return LlmRequest(contents=[
        types.Content(role="user", parts=[types.Part(text="do the thing")]),
        types.Content(role="user", parts=[types.Part(
            function_response=types.FunctionResponse(
                id="c1", name="run_bash",
                response={"stdout": "x" * payload_chars}))]),
        types.Content(role="user", parts=[types.Part(text="latest ask")]),
    ])


def main() -> int:
    # ---- calibration math ------------------------------------------------
    p = _plugin()
    p._note_usage("s1", 1000, 1300)
    c1 = p._correction("s1")
    check("drift learned (smoothed toward 1.3)", 1.1 < c1 < 1.3, c1)
    p._note_usage("s1", 1000, 1300)
    check("smoothing converges upward", p._correction("s1") > c1)
    p2 = _plugin()
    p2._note_usage("s1", 1000, 99999)
    check("ratio clamped (<=3.0 pre-smoothing)", p2._correction("s1") <= 2.0)
    check("unknown session -> 1.0", p2._correction("other") == 1.0)

    # ---- calibration feeds the DECISION ---------------------------------
    # NON-evictable bloat (plain text — the rewriter can't touch it): raw
    # ~1500 tokens sits below reject (1900), but a learned correction of
    # 1.5 -> ~2250 must trip the reject line. This is the incident: the
    # estimator's undercount hid a request the API would refuse.
    def _text_request(chars):
        from google.adk.models.llm_request import LlmRequest
        from google.genai import types

        return LlmRequest(contents=[
            types.Content(role="user", parts=[types.Part(text="x" * chars)]),
            types.Content(role="user", parts=[types.Part(text="latest ask")]),
        ])

    p3 = _plugin(max_tokens=2000, pressure_pct=0)  # ladder off; isolate reject
    p3._cal["s1"] = 1.5
    res = asyncio.run(p3.before_model_callback(
        callback_context=object(), llm_request=_text_request(5600)))
    check("corrected estimate can trigger the reject line",
          res is not None, "no rejection returned")
    p3b = _plugin(max_tokens=2000, pressure_pct=0)
    resb = asyncio.run(p3b.before_model_callback(
        callback_context=object(), llm_request=_text_request(5600)))
    check("same request WITHOUT drift passes (A/B)", resb is None)

    # ---- pressure ladder -------------------------------------------------
    # ~1750 tokens: above pressure (85% of 2000 = 1700), below reject (1900).
    p4 = _plugin(max_tokens=2000, pressure_pct=85)
    req4 = _request(6800)
    res4 = asyncio.run(p4.before_model_callback(
        callback_context=object(), llm_request=req4))
    fr = req4.contents[1].parts[0].function_response.response
    check("ladder fires between WARN and REJECT: no refusal", res4 is None)
    check("ladder rewrote the fat result",
          isinstance(fr, dict) and fr.get("status") in ("cleared", "summarized"),
          fr if not isinstance(fr, dict) else fr.get("status"))

    # ladder disabled -> fat result untouched at the same size
    p5 = _plugin(max_tokens=2000, pressure_pct=0)
    req5 = _request(6800)
    asyncio.run(p5.before_model_callback(
        callback_context=object(), llm_request=req5))
    fr5 = req5.contents[1].parts[0].function_response.response
    check("A/B ladder off: result untouched below reject",
          isinstance(fr5, dict) and "stdout" in fr5)

    # ---- incident replay (128K->219K shape, unit scale) ------------------
    # One turn, many model calls, each adding fat tool results. Unguarded,
    # the request sails past the window; the ladder must hold EVERY call
    # under 90% of effective without a single refusal.
    from adk_cc.permissions.token_counter import estimate_request_tokens
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    def _fat(i, chars=1600):
        return types.Content(role="user", parts=[types.Part(
            function_response=types.FunctionResponse(
                id=f"g{i}", name="run_bash",
                response={"stdout": f"call {i}: " + "y" * chars}))])

    def _replay(plugin):
        contents = [types.Content(role="user",
                                  parts=[types.Part(text="z" * 4800)])]
        peak, refusals = 0, 0
        for i in range(8):
            contents.append(_fat(i))
            contents.append(types.Content(
                role="user", parts=[types.Part(text=f"step {i} ok")]))
            req = LlmRequest(contents=contents)
            res = None
            if plugin is not None:
                res = asyncio.run(plugin.before_model_callback(
                    callback_context=object(), llm_request=req))
                contents = req.contents  # carry rewrites forward
            if res is not None:
                refusals += 1
            peak = max(peak, estimate_request_tokens(
                LlmRequest(contents=contents)))
        return peak, refusals

    unguarded_peak, _ = _replay(None)
    check("replay A/B: unguarded run blows the window",
          unguarded_peak > 2000, unguarded_peak)

    p7 = _plugin(max_tokens=2000, pressure_pct=85)
    p7._cal["s1"] = 1.15  # the incident's learned undercount
    guarded_peak, refusals = _replay(p7)
    check("replay: ladder holds every call under 90%",
          guarded_peak < int(0.90 * 2000), guarded_peak)
    check("replay: zero refusals across the whole turn", refusals == 0,
          refusals)

    # ---- after_model wiring ---------------------------------------------
    p6 = _plugin()
    req6 = _request(400)
    asyncio.run(p6.before_model_callback(
        callback_context=object(), llm_request=req6))

    class Usage:
        prompt_token_count = 500

    class Resp:
        usage_metadata = Usage()

    asyncio.run(p6.after_model_callback(
        callback_context=object(), llm_response=Resp()))
    check("after_model learns from usage_metadata",
          p6._correction("s1") != 1.0, p6._correction("s1"))

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
