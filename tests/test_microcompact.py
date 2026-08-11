"""Microcompaction rewrite logic — both shapes, summaries, edges. Model-free:
summaries are exercised through a monkeypatched `result_summaries.summarize`;
ADK_CC_RESULT_SUMMARIES=0 keeps the legacy stub tests off the network."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.genai import types

from adk_cc.context import result_summaries
from adk_cc.plugins import microcompact
from adk_cc.plugins.microcompact import (
    MicrocompactPlugin,
    _STUB_NOTE,
    rewrite_request,
)

# Legacy stub tests run with summaries OFF (no model, no network).
os.environ.setdefault("ADK_CC_RESULT_SUMMARIES", "0")


def _foreign_text(n_chars=8000, agent="Explore", tool="web_fetch"):
    return types.Part(text=(
        f"[{agent}] `{tool}` tool returned result: " + "{'content': '"
        + "w" * n_chars + "'}"))


class _FakeSummarize:
    """Stands in for result_summaries.summarize; counts calls."""

    def __init__(self, reply="CONDENSED FINDINGS"):
        self.reply = reply
        self.calls = 0

    async def __call__(self, text, *, tool="tool"):
        self.calls += 1
        return self.reply


def _resp_part(name: str, payload: dict):
    return types.Part(
        function_response=types.FunctionResponse(id=f"{name}-1", name=name, response=payload)
    )


def _call_part(name: str):
    return types.Part(function_call=types.FunctionCall(id=f"{name}-1", name=name, args={}))


def _req(parts):
    return SimpleNamespace(
        contents=[types.Content(role="user", parts=parts)],
        config=None,
    )


def _big(n=4000):
    return {"output": "x" * n}


def _run(req):
    plugin = MicrocompactPlugin()
    asyncio.run(plugin.before_model_callback(callback_context=SimpleNamespace(), llm_request=req))


def _is_stub(part) -> bool:
    r = part.function_response.response
    return isinstance(r, dict) and r.get("note") == _STUB_NOTE


def _setenv(**kw):
    for k, v in kw.items():
        os.environ[k] = str(v)


def _clearenv(*keys):
    for k in keys:
        os.environ.pop(k, None)


def test_enabled_by_default_since_incident():
    # Default flipped ON after the 2026-08-02 boundary overflow: a
    # disabled-by-default rewriter was a window footgun.
    _clearenv("ADK_CC_MICROCOMPACT")
    parts = [_resp_part("read_file", _big()) for _ in range(6)]
    req = _req(parts)
    _run(req)
    assert any(_is_stub(p) for p in req.contents[0].parts), "default must rewrite"


def test_explicit_zero_is_inert():
    _setenv(ADK_CC_MICROCOMPACT=0)
    try:
        parts = [_resp_part("read_file", _big()) for _ in range(6)]
        req = _req(parts)
        _run(req)
        assert not any(_is_stub(p) for p in req.contents[0].parts)
    finally:
        _clearenv("ADK_CC_MICROCOMPACT")


def test_evicts_old_large_keeps_recent():
    _setenv(ADK_CC_MICROCOMPACT=1, ADK_CC_MICROCOMPACT_KEEP_RECENT=2,
            ADK_CC_MICROCOMPACT_MIN_TOKENS=100)
    try:
        parts = [_resp_part("read_file", _big()) for _ in range(5)]
        req = _req(parts)
        _run(req)
        stubbed = [i for i, p in enumerate(parts) if _is_stub(p)]
        # 5 results, keep last 2 → first 3 evicted
        assert stubbed == [0, 1, 2], stubbed
        assert not _is_stub(parts[3]) and not _is_stub(parts[4])
    finally:
        _clearenv("ADK_CC_MICROCOMPACT", "ADK_CC_MICROCOMPACT_KEEP_RECENT",
                  "ADK_CC_MICROCOMPACT_MIN_TOKENS")


def test_small_results_kept_even_when_old():
    _setenv(ADK_CC_MICROCOMPACT=1, ADK_CC_MICROCOMPACT_KEEP_RECENT=1,
            ADK_CC_MICROCOMPACT_MIN_TOKENS=2000)
    try:
        parts = [_resp_part("read_file", {"output": "small"}) for _ in range(5)]
        req = _req(parts)
        _run(req)
        assert not any(_is_stub(p) for p in parts), "below min_tokens → keep"
    finally:
        _clearenv("ADK_CC_MICROCOMPACT", "ADK_CC_MICROCOMPACT_KEEP_RECENT",
                  "ADK_CC_MICROCOMPACT_MIN_TOKENS")


def test_non_compactable_tools_untouched():
    _setenv(ADK_CC_MICROCOMPACT=1, ADK_CC_MICROCOMPACT_KEEP_RECENT=0,
            ADK_CC_MICROCOMPACT_MIN_TOKENS=100)
    try:
        # wiki_read / write_plan / ask_user_question are NOT in the allow-list
        parts = [_resp_part("wiki_read", _big()), _resp_part("write_plan", _big()),
                 _resp_part("ask_user_question", _big())]
        req = _req(parts)
        _run(req)
        assert not any(_is_stub(p) for p in parts), "non-compactable must be kept"
    finally:
        _clearenv("ADK_CC_MICROCOMPACT", "ADK_CC_MICROCOMPACT_KEEP_RECENT",
                  "ADK_CC_MICROCOMPACT_MIN_TOKENS")


def test_pairing_preserved_call_and_id_intact():
    _setenv(ADK_CC_MICROCOMPACT=1, ADK_CC_MICROCOMPACT_KEEP_RECENT=0,
            ADK_CC_MICROCOMPACT_MIN_TOKENS=100)
    try:
        call = _call_part("read_file")
        resp = _resp_part("read_file", _big())
        req = _req([call, resp])
        _run(req)
        # the function_call part is untouched; the response is stubbed but keeps id+name
        assert req.contents[0].parts[0].function_call.name == "read_file"
        fr = req.contents[0].parts[1].function_response
        assert fr.id == "read_file-1" and fr.name == "read_file"
        assert _is_stub(req.contents[0].parts[1])
    finally:
        _clearenv("ADK_CC_MICROCOMPACT", "ADK_CC_MICROCOMPACT_KEEP_RECENT",
                  "ADK_CC_MICROCOMPACT_MIN_TOKENS")


def test_idempotent_no_double_evict():
    _setenv(ADK_CC_MICROCOMPACT=1, ADK_CC_MICROCOMPACT_KEEP_RECENT=1,
            ADK_CC_MICROCOMPACT_MIN_TOKENS=100)
    try:
        parts = [_resp_part("grep", _big()) for _ in range(3)]
        req = _req(parts)
        _run(req)
        _run(req)  # second pass: already-stubbed are skipped, no error
        stubbed = sum(1 for p in parts if _is_stub(p))
        assert stubbed == 2, stubbed  # 3 results, keep last 1 → 2 stubbed
    finally:
        _clearenv("ADK_CC_MICROCOMPACT", "ADK_CC_MICROCOMPACT_KEEP_RECENT",
                  "ADK_CC_MICROCOMPACT_MIN_TOKENS")


# ---- foreign-text shape (the incident's costume) -------------------------


def test_foreign_results_rewritten_oldest_first_newest_kept():
    _setenv(ADK_CC_MICROCOMPACT=1, ADK_CC_MICROCOMPACT_KEEP_RECENT=2,
            ADK_CC_MICROCOMPACT_MIN_TOKENS=100)
    try:
        parts = [types.Part(text="For context:")] + [_foreign_text() for _ in range(5)]
        req = _req(parts)
        _run(req)
        rewritten = [i for i, p in enumerate(parts)
                     if p.text and "chars evicted" in p.text]
        assert rewritten == [1, 2, 3], rewritten     # oldest 3 of 5; newest 2 kept
        assert parts[4].text.startswith("[Explore]") and "evicted" not in parts[4].text
        assert parts[0].text == "For context:"       # non-result text untouched
    finally:
        _clearenv("ADK_CC_MICROCOMPACT", "ADK_CC_MICROCOMPACT_KEEP_RECENT",
                  "ADK_CC_MICROCOMPACT_MIN_TOKENS")


def test_prose_and_near_miss_text_never_touched():
    _setenv(ADK_CC_MICROCOMPACT=1, ADK_CC_MICROCOMPACT_KEEP_RECENT=0,
            ADK_CC_MICROCOMPACT_MIN_TOKENS=100)
    try:
        prose = types.Part(text="A genuine long user message. " * 500)
        said = types.Part(text="[Explore] said: " + "important analysis " * 500)
        small = _foreign_text(100)                    # matching but tiny
        req = _req([prose, said, small])
        _run(req)
        assert prose.text.startswith("A genuine")
        assert said.text.startswith("[Explore] said:")
        assert "evicted" not in small.text
    finally:
        _clearenv("ADK_CC_MICROCOMPACT", "ADK_CC_MICROCOMPACT_KEEP_RECENT",
                  "ADK_CC_MICROCOMPACT_MIN_TOKENS")


def test_foreign_idempotent_across_passes():
    _setenv(ADK_CC_MICROCOMPACT=1, ADK_CC_MICROCOMPACT_KEEP_RECENT=1,
            ADK_CC_MICROCOMPACT_MIN_TOKENS=100)
    try:
        parts = [_foreign_text() for _ in range(3)]
        req = _req(parts)
        _run(req)
        first = [p.text for p in parts]
        _run(req)
        assert [p.text for p in parts] == first, "second pass must change nothing"
    finally:
        _clearenv("ADK_CC_MICROCOMPACT", "ADK_CC_MICROCOMPACT_KEEP_RECENT",
                  "ADK_CC_MICROCOMPACT_MIN_TOKENS")


def test_regex_handles_odd_agent_and_tool_names():
    _setenv(ADK_CC_MICROCOMPACT=1, ADK_CC_MICROCOMPACT_KEEP_RECENT=0,
            ADK_CC_MICROCOMPACT_MIN_TOKENS=100)
    try:
        odd = [_foreign_text(agent="deep research v2", tool="web-fetch_2"),
               _foreign_text(agent="Exploreré", tool="grep")]
        req = _req(odd)
        _run(req)
        assert all("chars evicted" in p.text for p in odd)
    finally:
        _clearenv("ADK_CC_MICROCOMPACT", "ADK_CC_MICROCOMPACT_KEEP_RECENT",
                  "ADK_CC_MICROCOMPACT_MIN_TOKENS")


# ---- summaries ------------------------------------------------------------


def test_summary_substitution_both_shapes():
    fake = _FakeSummarize()
    orig = result_summaries.summarize
    result_summaries.summarize = fake
    try:
        parts = [_resp_part("web_fetch", _big(9000)), _foreign_text(9000),
                 _resp_part("web_fetch", _big(9000))]
        req = _req(parts)
        asyncio.run(rewrite_request(req, keep_recent=1, min_tokens=100,
                                    allow_summaries=True))
        fr = parts[0].function_response.response
        assert fr["status"] == "summarized" and fr["summary"] == "CONDENSED FINDINGS"
        assert "condensed from" in fr["note"]
        assert parts[1].text.startswith("[condensed from ")
        assert "CONDENSED FINDINGS" in parts[1].text
        assert isinstance(parts[2].function_response.response, dict)             and "output" in parts[2].function_response.response  # newest kept
        assert fake.calls == 2
    finally:
        result_summaries.summarize = orig


def test_summary_failure_falls_back_to_stub():
    fake = _FakeSummarize(reply=None)
    orig = result_summaries.summarize
    result_summaries.summarize = fake
    try:
        parts = [_resp_part("web_fetch", _big(9000)), _foreign_text(9000)]
        req = _req(parts)
        asyncio.run(rewrite_request(req, keep_recent=0, min_tokens=100,
                                    allow_summaries=True))
        assert parts[0].function_response.response.get("status") == "cleared"
        assert "chars evicted" in parts[1].text
    finally:
        result_summaries.summarize = orig


def test_summarized_parts_idempotent():
    fake = _FakeSummarize()
    orig = result_summaries.summarize
    result_summaries.summarize = fake
    try:
        parts = [_foreign_text(9000), _resp_part("web_fetch", _big(9000))]
        req = _req(parts)
        asyncio.run(rewrite_request(req, keep_recent=0, min_tokens=100,
                                    allow_summaries=True))
        calls_after_first = fake.calls
        asyncio.run(rewrite_request(req, keep_recent=0, min_tokens=100,
                                    allow_summaries=True))
        assert fake.calls == calls_after_first, "rewritten parts must be skipped"
    finally:
        result_summaries.summarize = orig


def test_budget_stops_early():
    parts = [_resp_part("web_fetch", _big(40_000)) for _ in range(5)]
    req = _req(parts)
    stats = asyncio.run(rewrite_request(req, keep_recent=0, min_tokens=100,
                                        allow_summaries=False,
                                        budget_tokens=12_000))
    # each result ≈ 10k tokens; 12k budget → two rewrites then stop
    assert stats["rewritten"] == 2, stats
    assert not _is_stub(parts[3]) and not _is_stub(parts[4])


def test_keep_recent_counts_across_both_shapes():
    parts = [_resp_part("web_fetch", _big(9000)),       # oldest
             _foreign_text(9000),
             _resp_part("web_fetch", _big(9000)),
             _foreign_text(9000)]                       # newest
    req = _req(parts)
    asyncio.run(rewrite_request(req, keep_recent=2, min_tokens=100,
                                allow_summaries=False))
    assert _is_stub(parts[0])
    assert "chars evicted" in parts[1].text
    assert not _is_stub(parts[2]), "2nd newest kept regardless of shape"
    assert "evicted" not in parts[3].text, "newest kept regardless of shape"


def _fat_call(name="write_file", cid="w-1", body_chars=8000):
    return types.Part(function_call=types.FunctionCall(
        id=cid, name=name,
        args={"path": "/tmp/out.html", "content": "<html>" + "b" * body_chars}))


def test_completed_call_args_elided():
    parts = [_fat_call(cid="w-1"),
             _resp_part("write_file", {"status": "ok"}),
             types.Part(text="later chatter")]
    parts[1].function_response.id = "w-1"
    req = _req(parts)
    stats = asyncio.run(rewrite_request(req, keep_recent=0, min_tokens=100,
                                        allow_summaries=False))
    args = parts[0].function_call.args
    assert args["content"].startswith("[adk-cc elided call argument:"), args["content"][:60]
    assert args["path"] == "/tmp/out.html", "small sibling arg untouched"
    assert stats["rewritten"] >= 1 and stats["freed"] > 1500, stats


def test_pending_call_args_never_touched():
    # No matching function_response id → parked/pending. Even the guard's
    # desperation pass (keep_recent=0, min_tokens tiny) must not touch it.
    parts = [_fat_call(cid="pending-1")]
    req = _req(parts)
    stats = asyncio.run(rewrite_request(req, keep_recent=0, min_tokens=16,
                                        allow_summaries=False))
    assert "b" * 100 in parts[0].function_call.args["content"]
    assert stats["rewritten"] == 0, stats


def test_parked_confirmation_call_args_never_touched():
    # #119 name-aware: the gate closes a parked call with an interim
    # needs_confirmation response using the SAME id. That is not an answer —
    # even the guard's desperation settings must keep the args whole so the
    # model can still quote what it is about to do while the user decides.
    parts = [_fat_call(cid="w-1"),
             _resp_part("write_file", {"status": "needs_confirmation"})]
    parts[1].function_response.id = "w-1"
    req = _req(parts)
    stats = asyncio.run(rewrite_request(req, keep_recent=0, min_tokens=16,
                                        allow_summaries=False))
    assert "b" * 100 in parts[0].function_call.args["content"]
    assert stats["rewritten"] == 0, stats


def test_call_args_idempotent():
    parts = [_fat_call(cid="w-1"), _resp_part("write_file", {"status": "ok"})]
    parts[1].function_response.id = "w-1"
    req = _req(parts)
    asyncio.run(rewrite_request(req, keep_recent=0, min_tokens=100,
                                allow_summaries=False))
    once = parts[0].function_call.args["content"]
    stats2 = asyncio.run(rewrite_request(req, keep_recent=0, min_tokens=100,
                                         allow_summaries=False))
    assert parts[0].function_call.args["content"] == once
    assert stats2["rewritten"] == 0, stats2


def test_call_args_small_below_threshold_kept():
    parts = [_fat_call(cid="w-1", body_chars=300),
             _resp_part("write_file", {"status": "ok"})]
    parts[1].function_response.id = "w-1"
    req = _req(parts)
    stats = asyncio.run(rewrite_request(req, keep_recent=0, min_tokens=100,
                                        allow_summaries=False))
    assert "b" * 100 in parts[0].function_call.args["content"]
    assert stats["rewritten"] == 0, stats


def test_keep_recent_shields_newest_call_shape():
    parts = []
    for i in range(3):
        parts.append(_fat_call(cid=f"w-{i}"))
        rp = _resp_part("plan", {"status": "ok"})  # non-compactable response
        rp.function_response.id = f"w-{i}"
        parts.append(rp)
    req = _req(parts)
    asyncio.run(rewrite_request(req, keep_recent=1, min_tokens=100,
                                allow_summaries=False))
    assert parts[0].function_call.args["content"].startswith("[adk-cc elided")
    assert parts[2].function_call.args["content"].startswith("[adk-cc elided")
    assert "b" * 100 in parts[4].function_call.args["content"], \
        "newest call kept under keep_recent=1"


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__[5:]}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__[5:]}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__[5:]}: {type(e).__name__}: {e}")
    print("\nall microcompact tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
