"""Phase 2 tests: microcompaction eviction logic. Model-free."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.genai import types

from adk_cc.plugins.microcompact import MicrocompactPlugin, _STUB_NOTE


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


def test_disabled_by_default_is_noop():
    _clearenv("ADK_CC_MICROCOMPACT")
    parts = [_resp_part("read_file", _big()) for _ in range(6)]
    req = _req(parts)
    _run(req)
    assert not any(_is_stub(p) for p in req.contents[0].parts), "must be inert when off"


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
