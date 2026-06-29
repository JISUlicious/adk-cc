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

    # --- resolve precedence ---
    _set_env(None)
    check("no env, no cfg → None", resolve_max_output_tokens() is None)
    _set_env("4096")
    check("env → int", resolve_max_output_tokens() == 4096)
    _set_env("nope")
    check("invalid env → None", resolve_max_output_tokens() is None)
    _set_env("0")
    check("non-positive env → None", resolve_max_output_tokens() is None)

    cfg = ModelEndpointConfig(name="x", model="openai/x", api_base="http://x", api_key_env="", max_tokens=1234)
    _set_env("4096")
    check("per-endpoint overrides env", resolve_max_output_tokens(cfg) == 1234)
    _set_env(None)
    check("per-endpoint alone", resolve_max_output_tokens(cfg) == 1234)
    cfg_none = ModelEndpointConfig(name="y", model="openai/y", api_base="http://y", api_key_env="")
    _set_env("2048")
    check("cfg without max_tokens falls back to env", resolve_max_output_tokens(cfg_none) == 2048)

    # --- _build_litellm threads max_tokens into the delegate ---
    _set_env(None)
    sel = SelectableLlm(default_model_id="m")
    delegate = sel._build_litellm(cfg)  # cfg.max_tokens=1234, keyless
    check("LiteLlm delegate carries max_tokens", getattr(delegate, "_additional_args", {}).get("max_tokens") == 1234)
    delegate2 = sel._build_litellm(cfg_none)  # no cfg max_tokens, no env
    check("no cap → delegate has no max_tokens", "max_tokens" not in getattr(delegate2, "_additional_args", {}))

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
        return out, msgs

    out, msgs = asyncio.run(run())
    check("all responses still yielded (passthrough)", len(out) == 2)
    check("MAX_TOKENS finish logs a warning", any("MAX_TOKENS" in m for m in msgs))

    print(f"\nmax-output-tokens: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
