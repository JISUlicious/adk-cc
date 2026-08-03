"""Result-summaries engine: cache semantics, single-flight, degradation.

Model-free: `_resolve_model` is monkeypatched to scripted fakes. The engine's
contract — compute once per digest, share concurrent calls, survive restarts
via disk, fail to None quickly and not retry a failing digest for a while —
is what the request rewriter and the guard lean on.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_result_summaries.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
os.environ.pop("ADK_CC_RESULT_SUMMARIES", None)   # default (on) under test

from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.genai import types  # noqa: E402

from adk_cc.context import result_summaries as rs  # noqa: E402

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


class _FakeModel:
    def __init__(self, reply="THE SUMMARY", delay=0.0):
        self.reply = reply
        self.delay = delay
        self.calls = 0

    async def generate_content_async(self, req, stream=False):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        yield LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text=self.reply)]), partial=False)


def _with_model(model):
    rs._reset_for_test()
    rs._resolve_model = lambda: model  # type: ignore
    return model


async def _scenario(tmp):
    os.environ["ADK_CC_DESKTOP_DATA"] = tmp

    # --- cache: one model call per digest, ever ------------------------
    m = _with_model(_FakeModel())
    out1 = await rs.summarize("x" * 20_000, tool="web_fetch")
    out2 = await rs.summarize("x" * 20_000, tool="web_fetch")
    check("summary returned", out1 == "THE SUMMARY", out1)
    check("second call is a cache hit (no second model call)",
          out2 == "THE SUMMARY" and m.calls == 1, m.calls)
    out3 = await rs.summarize("y" * 20_000, tool="web_fetch")
    check("different content -> different digest -> second call",
          out3 == "THE SUMMARY" and m.calls == 2, m.calls)

    # --- disk write-through survives a process restart ------------------
    m2 = _with_model(_FakeModel(reply="NEVER USED"))
    out4 = await rs.summarize("x" * 20_000, tool="web_fetch")
    check("restart (reset) still hits the DISK cache",
          out4 == "THE SUMMARY" and m2.calls == 0, (out4, m2.calls))

    # --- single-flight: concurrent same-digest shares one call ----------
    m3 = _with_model(_FakeModel(delay=0.1))
    a, b = await asyncio.gather(
        rs.summarize("z" * 20_000, tool="grep"),
        rs.summarize("z" * 20_000, tool="grep"))
    check("concurrent calls for one digest share one model call",
          a == b == "THE SUMMARY" and m3.calls == 1, m3.calls)

    # --- timeout -> None, and the digest is negative-cached -------------
    os.environ["ADK_CC_COMPACTION_TIMEOUT_S"] = "0.05"
    try:
        m4 = _with_model(_FakeModel(delay=1.0))
        out5 = await rs.summarize("t" * 20_000, tool="web_fetch")
        check("a slow summarizer times out to None", out5 is None, out5)
        out6 = await rs.summarize("t" * 20_000, tool="web_fetch")
        check("the failed digest is not retried immediately",
              out6 is None and m4.calls == 1, m4.calls)
    finally:
        os.environ.pop("ADK_CC_COMPACTION_TIMEOUT_S", None)

    # --- rate limits retry with backoff, then succeed --------------------
    rs._RETRY_SLEEPS = (0.01, 0.01)          # fast schedule under test

    class _RL(Exception):
        status_code = 429

    class _FlakyModel:
        def __init__(self):
            self.calls = 0

        async def generate_content_async(self, req, stream=False):
            self.calls += 1
            if self.calls <= 2:
                raise _RL("429 too many requests")
            yield LlmResponse(content=types.Content(
                role="model", parts=[types.Part(text="AFTER RETRIES")]),
                partial=False)

    mf = _with_model(_FlakyModel())
    out_rl = await rs.summarize("r" * 20_000, tool="web_fetch")
    check("rate-limited attempts retry and then succeed",
          out_rl == "AFTER RETRIES" and mf.calls == 3, (out_rl, mf.calls))

    class _AlwaysRL:
        def __init__(self):
            self.calls = 0

        async def generate_content_async(self, req, stream=False):
            self.calls += 1
            raise _RL("429")
            yield  # pragma: no cover

    ma = _with_model(_AlwaysRL())
    out_rl2 = await rs.summarize("s" * 20_000, tool="web_fetch")
    check("exhausted rate-limit retries degrade to None",
          out_rl2 is None and ma.calls == 3, (out_rl2, ma.calls))

    # --- empty reply degrades the same way ------------------------------
    m5 = _with_model(_FakeModel(reply=""))
    check("empty model reply -> None",
          await rs.summarize("e" * 20_000) is None)

    # --- kill-switch -----------------------------------------------------
    os.environ["ADK_CC_RESULT_SUMMARIES"] = "0"
    try:
        m6 = _with_model(_FakeModel())
        out7 = await rs.summarize("k" * 20_000)
        check("ADK_CC_RESULT_SUMMARIES=0 -> None, zero model calls",
              out7 is None and m6.calls == 0, (out7, m6.calls))
    finally:
        os.environ.pop("ADK_CC_RESULT_SUMMARIES", None)

    # --- LRU bound -------------------------------------------------------
    _with_model(_FakeModel())
    for i in range(rs._MEM_MAX + 20):
        rs._mem_put(f"d{i}", "s")
    check("memory cache is bounded", len(rs._mem) == rs._MEM_MAX, len(rs._mem))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        saved = os.environ.get("ADK_CC_DESKTOP_DATA")
        try:
            asyncio.run(_scenario(tmp))
        finally:
            if saved is not None:
                os.environ["ADK_CC_DESKTOP_DATA"] = saved
            else:
                os.environ.pop("ADK_CC_DESKTOP_DATA", None)
            rs._reset_for_test()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
