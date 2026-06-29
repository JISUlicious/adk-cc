"""Tier 2 prevention: configurable max_output_tokens + finish_reason=MAX_TOKENS
detection. Model-free.

Run: PYTHONPATH=agents .venv/bin/python tests/test_max_output_tokens.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

_passed = _failed = 0


def check(name, ok):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    _passed += 1 if ok else 0
    _failed += 0 if ok else 1


def _set_env(val):
    if val is None:
        os.environ.pop("ADK_CC_MAX_OUTPUT_TOKENS", None)
    else:
        os.environ["ADK_CC_MAX_OUTPUT_TOKENS"] = val


def main() -> int:
    from adk_cc.models import ModelEndpointConfig
    from adk_cc.models.selectable import resolve_max_output_tokens, SelectableLlm

    # --- resolve precedence (default-on, like Claude Code) ---
    _set_env(None)
    check("no env, no cfg → default 8192", resolve_max_output_tokens() == 8192)
    _set_env("4096")
    check("env → int", resolve_max_output_tokens() == 4096)
    _set_env("nope")
    check("invalid env → default", resolve_max_output_tokens() == 8192)
    _set_env("0")
    check("env=0 → uncapped (opt out)", resolve_max_output_tokens() is None)

    cfg = ModelEndpointConfig(name="x", model="openai/x", api_base="http://x", api_key_env="", max_tokens=1234)
    _set_env("4096")
    check("per-endpoint overrides env", resolve_max_output_tokens(cfg) == 1234)
    _set_env(None)
    check("per-endpoint alone", resolve_max_output_tokens(cfg) == 1234)
    cfg_none = ModelEndpointConfig(name="y", model="openai/y", api_base="http://y", api_key_env="")
    _set_env("2048")
    check("cfg without max_tokens falls back to env", resolve_max_output_tokens(cfg_none) == 2048)

    # --- _build_litellm threads the cap into the delegate ---
    _set_env(None)
    sel = SelectableLlm(default_model_id="m")
    delegate = sel._build_litellm(cfg)  # cfg.max_tokens=1234, keyless
    check("delegate carries per-endpoint max_tokens", getattr(delegate, "_additional_args", {}).get("max_tokens") == 1234)
    delegate_def = sel._build_litellm(cfg_none)  # no cfg, no env → default 8192
    check("delegate gets the default cap (8192)", getattr(delegate_def, "_additional_args", {}).get("max_tokens") == 8192)
    _set_env("0")
    delegate_unc = sel._build_litellm(cfg_none)  # env=0 → uncapped
    check("env=0 → uncapped delegate (no max_tokens)", "max_tokens" not in getattr(delegate_unc, "_additional_args", {}))
    _set_env(None)

    # --- escalation: the cap raises after the model truncates ---
    from adk_cc.models.selectable import escalated_max_output_tokens
    os.environ.pop("ADK_CC_MAX_OUTPUT_TOKENS_ESCALATED", None)
    check("escalated default = 32768", escalated_max_output_tokens() == 32768)
    s2 = SelectableLlm(default_model_id="m")
    check("base cap before escalation", s2._effective_cap(8192) == 8192)
    s2._escalated = True
    check("cap raises after escalation", s2._effective_cap(8192) == 32768)
    check("escalation never lowers a cap", s2._effective_cap(65536) == 65536)
    check("escalation no-op on an uncapped base", s2._effective_cap(None) is None)

    # --- finish_reason=MAX_TOKENS logs a root-cause warning ---
    class _FR:
        def __init__(self, name): self.name = name

    class _Resp:
        def __init__(self, fr): self.finish_reason = fr

    class _Delegate:
        async def generate_content_async(self, llm_request, stream=False):
            yield _Resp(_FR("STOP"))
            yield _Resp(_FR("MAX_TOKENS"))

    async def run():
        s = SelectableLlm(default_model_id="m")
        s._resolve_delegate = lambda: _Delegate()
        msgs: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda r: msgs.append(r.getMessage())
        lg = logging.getLogger("adk_cc.models.selectable")
        lg.addHandler(handler); old = lg.level; lg.setLevel(logging.WARNING)
        try:
            out = [r async for r in s.generate_content_async(None)]
        finally:
            lg.removeHandler(handler); lg.setLevel(old)
        return out, msgs, s._escalated

    out, msgs, escalated_after = asyncio.run(run())
    check("all responses still yielded (passthrough)", len(out) == 2)
    check("MAX_TOKENS finish logs a warning", any("MAX_TOKENS" in m for m in msgs))
    check("MAX_TOKENS finish escalates the cap", escalated_after is True)

    print(f"\nmax-output-tokens: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
