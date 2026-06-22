"""Phase 4 tests: ContextGuard reserve/ladder math + payload-inclusive counter."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.genai import types

from adk_cc.permissions.token_counter import (
    estimate_prompt_tokens,
    estimate_prompt_tokens_full,
)
from adk_cc.plugins.context_guard import _normalize_ladder


def _guard(**env):
    # set env, build a fresh plugin, restore env
    keys = ["ADK_CC_MAX_CONTEXT_TOKENS", "ADK_CC_CONTEXT_RESERVE_TOKENS",
            "ADK_CC_CONTEXT_WARN_TOKENS", "ADK_CC_CONTEXT_REJECT_TOKENS",
            "ADK_CC_CONTEXT_COUNT_TOOL_PAYLOADS", "ADK_CC_COMPACTION_TOKEN_THRESHOLD"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in env.items():
            os.environ[k] = str(v)
        from adk_cc.plugins.context_guard import ContextGuardPlugin
        return ContextGuardPlugin()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_disabled_when_max_unset():
    g = _guard()
    assert g._max is None and g._warn is None and g._reject is None


def test_no_reserve_preserves_legacy_defaults():
    g = _guard(ADK_CC_MAX_CONTEXT_TOKENS=200000)
    assert g._reserve == 0 and g._effective == 200000
    assert g._warn == 150000 and g._reject == 190000  # == MAX*0.75 / 0.95


def test_reserve_shrinks_effective_and_ladder():
    g = _guard(ADK_CC_MAX_CONTEXT_TOKENS=200000, ADK_CC_CONTEXT_RESERVE_TOKENS=20000)
    assert g._effective == 180000
    assert g._warn == 135000 and g._reject == 171000  # 180k * .75 / .95
    assert g._warn < g._reject <= g._effective


def test_explicit_warn_reject_override():
    g = _guard(ADK_CC_MAX_CONTEXT_TOKENS=100000, ADK_CC_CONTEXT_RESERVE_TOKENS=10000,
               ADK_CC_CONTEXT_WARN_TOKENS=42000, ADK_CC_CONTEXT_REJECT_TOKENS=80000)
    assert g._warn == 42000 and g._reject == 80000


def test_count_tool_payloads_flag():
    assert _guard(ADK_CC_MAX_CONTEXT_TOKENS=1000)._count_tool_payloads is False
    assert _guard(ADK_CC_MAX_CONTEXT_TOKENS=1000,
                  ADK_CC_CONTEXT_COUNT_TOOL_PAYLOADS=1)._count_tool_payloads is True


# ---- enforced self-heal (_normalize_ladder) ----
def _inv(reserve, eff, warn, reject, max_tokens):
    assert 0 <= reserve < max_tokens, (reserve, max_tokens)
    assert eff == max_tokens - reserve
    assert 1 <= warn < reject <= eff, (warn, reject, eff)


def test_normalize_valid_passthrough():
    r, e, w, j, corr = _normalize_ladder(200000, 20000, None, None)
    assert (r, e, w, j) == (20000, 180000, 135000, 171000) and corr == []
    _inv(r, e, w, j, 200000)


def test_normalize_reserve_exceeding_max_is_clamped():
    r, e, w, j, corr = _normalize_ladder(1000, 5000, None, None)
    assert r == 999 and e == 1 and any("RESERVE" in c for c in corr)
    # degenerate tiny window still yields a valid (clamped) ladder
    assert 1 <= w <= j <= e


def test_normalize_inverted_warn_reject_is_fixed():
    # operator set WARN above REJECT → must be corrected to WARN < REJECT
    r, e, w, j, corr = _normalize_ladder(100000, 0, 90000, 50000)
    assert w < j, (w, j)
    assert any("WARN" in c for c in corr)
    _inv(r, e, w, j, 100000)


def test_normalize_reject_above_effective_is_clamped():
    r, e, w, j, corr = _normalize_ladder(100000, 10000, 50000, 999999)
    assert j <= e and any("REJECT" in c for c in corr)
    _inv(r, e, w, j, 100000)


def test_normalize_negative_reserve_clamped():
    r, e, w, j, corr = _normalize_ladder(100000, -5, None, None)
    assert r == 0 and any("RESERVE" in c for c in corr)


def test_guard_self_heals_inverted_env():
    # full integration through the plugin: inverted WARN/REJECT env → fixed
    g = _guard(ADK_CC_MAX_CONTEXT_TOKENS=100000,
               ADK_CC_CONTEXT_WARN_TOKENS=90000, ADK_CC_CONTEXT_REJECT_TOKENS=50000)
    assert g._warn < g._reject <= g._effective, (g._warn, g._reject, g._effective)


# ---- payload-inclusive estimator ----
def _req_with_tool_result(payload: dict, text: str = ""):
    parts = []
    if text:
        parts.append(types.Part(text=text))
    parts.append(types.Part(
        function_response=types.FunctionResponse(id="x", name="read_file", response=payload)))
    return SimpleNamespace(contents=[types.Content(role="user", parts=parts)])


def test_full_counts_tool_payload_but_base_ignores_it():
    big = {"output": "y" * 8000}  # ~2000 tokens of tool result
    req = _req_with_tool_result(big, text="hi")
    base = estimate_prompt_tokens(req)            # text-only → tiny
    full = estimate_prompt_tokens_full(req)       # includes the payload
    assert full > base + 1000, (base, full)
    assert base < 10  # "hi" only


def test_full_prefers_usage_metadata():
    req = _req_with_tool_result({"output": "z" * 8000})
    ev = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=4242))
    assert estimate_prompt_tokens_full(req, session_events=[ev]) == 4242


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
    print("\nall context-ladder tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
