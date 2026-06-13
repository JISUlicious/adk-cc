"""Tests for WikiRecallPlugin (plugins/wiki_recall.py, Phase 2).

Two behaviors: (1) recall injection on before_model — cheap, no model call,
appends a budgeted wiki slice to the system_instruction; (2) opt-in
auto-capture — spawn-early/persist-late extraction of durable facts into the
caller's inbox, mirroring SessionTitlePlugin (fake BaseLlm, no live model).
Hand-rolled.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from typing import AsyncGenerator

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from adk_cc.memory.page import Page
from adk_cc.memory.store import WikiStore
from adk_cc.plugins.wiki_recall import WikiRecallPlugin, _parse_capture


def _tenant_state(tenant="acme", user="alice"):
    return {"temp:tenant_context": SimpleNamespace(tenant_id=tenant, user_id=user)}


def _user_req(text: str) -> LlmRequest:
    return LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=text)])],
        config=types.GenerateContentConfig(),
    )


def _si_str(req: LlmRequest) -> str:
    si = req.config.system_instruction
    if si is None:
        return ""
    if isinstance(si, str):
        return si
    return " ".join(getattr(p, "text", "") or "" for p in (si if isinstance(si, list) else [si]))


# ---------------- recall injection (no model call) ----------------
def test_recall_injects_relevant_block():
    with tempfile.TemporaryDirectory() as root:
        os.environ["ADK_CC_WIKI_ROOT"] = root
        st = WikiStore.for_tenant("acme").ensure()
        st.write_domain_page(Page("openai", {"title": "OpenAI"}, "OpenAI builds GPT.\n"))
        plugin = WikiRecallPlugin()
        req = _user_req("tell me about OpenAI")
        cbctx = SimpleNamespace(session=SimpleNamespace(state=_tenant_state()))
        asyncio.run(plugin.before_model_callback(callback_context=cbctx, llm_request=req))
        si = _si_str(req)
        assert "Knowledge wiki" in si, si[:120]
        assert "openai" in si.lower()
    print("OK recall_injects_relevant_block")


def test_recall_surfaces_discrepancy():
    with tempfile.TemporaryDirectory() as root:
        os.environ["ADK_CC_WIKI_ROOT"] = root
        st = WikiStore.for_tenant("acme").ensure()
        st.write_domain_page(Page("gpt-4", {}, "Context window is 8k tokens.\n"))
        st.add_inbox("alice", "Context window is now 128k tokens.", topic="gpt-4")
        plugin = WikiRecallPlugin()
        req = _user_req("what is the gpt-4 context window")
        cbctx = SimpleNamespace(session=SimpleNamespace(state=_tenant_state()))
        asyncio.run(plugin.before_model_callback(callback_context=cbctx, llm_request=req))
        si = _si_str(req)
        assert "differ" in si.lower(), si
        assert "128k" in si and "8k" in si
    print("OK recall_surfaces_discrepancy")


def test_recall_skips_empty_query_and_missing_wiki():
    with tempfile.TemporaryDirectory() as root:
        os.environ["ADK_CC_WIKI_ROOT"] = root
        plugin = WikiRecallPlugin()
        # empty query → nothing injected
        req = LlmRequest(
            contents=[types.Content(role="model", parts=[types.Part(text="hi")])],
            config=types.GenerateContentConfig(),
        )
        cbctx = SimpleNamespace(session=SimpleNamespace(state=_tenant_state()))
        asyncio.run(plugin.before_model_callback(callback_context=cbctx, llm_request=req))
        assert _si_str(req) == ""
        # query present but tenant has no wiki dir yet → nothing injected
        req2 = _user_req("anything")
        cb2 = SimpleNamespace(session=SimpleNamespace(state=_tenant_state(tenant="never")))
        asyncio.run(plugin.before_model_callback(callback_context=cb2, llm_request=req2))
        assert _si_str(req2) == ""
    print("OK recall_skips_empty_query_and_missing_wiki")


def test_recall_preserves_existing_system_instruction():
    with tempfile.TemporaryDirectory() as root:
        os.environ["ADK_CC_WIKI_ROOT"] = root
        st = WikiStore.for_tenant("acme").ensure()
        st.write_domain_page(Page("openai", {}, "OpenAI builds GPT.\n"))
        plugin = WikiRecallPlugin()
        req = _user_req("about OpenAI")
        req.config.system_instruction = "BASE INSTRUCTION"
        cbctx = SimpleNamespace(session=SimpleNamespace(state=_tenant_state()))
        asyncio.run(plugin.before_model_callback(callback_context=cbctx, llm_request=req))
        si = _si_str(req)
        # recall is APPENDED (turn-volatile last), base stays first
        assert si.startswith("BASE INSTRUCTION"), si[:60]
        assert "Knowledge wiki" in si
    print("OK recall_preserves_existing_system_instruction")


# ---------------- auto-capture (fake model) ----------------
class _FakeLlm(BaseLlm):
    reply: str = "TOPIC: API rate limit\nThe production API allows 1000 requests/min."
    delay: float = 0.0
    explode: bool = False
    calls: int = 0

    async def generate_content_async(
        self, llm_request, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        type(self).calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.explode:
            raise RuntimeError("model down")
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=self.reply)])
        )


async def _make_ictx(user_text="our API allows 1000 req/min", llm: _FakeLlm = None):
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="t", user_id="u", state={})
    # create_session strips temp: keys; in a real run TenancyPlugin seeds this
    # live during the invocation. Inject it directly for the test.
    session.state["temp:tenant_context"] = SimpleNamespace(
        tenant_id="acme", user_id="alice"
    )
    agent = LlmAgent(name="t", model=llm or _FakeLlm(model="fake/model"))
    user_content = (
        types.Content(role="user", parts=[types.Part(text=user_text)])
        if user_text is not None else None
    )
    return InvocationContext(
        session_service=svc, invocation_id="inv-run",
        agent=agent, session=session, user_content=user_content,
    )


def test_autocapture_disabled_by_default():
    async def run():
        _FakeLlm.calls = 0
        os.environ.pop("ADK_CC_WIKI_AUTOCAPTURE", None)
        ictx = await _make_ictx()
        plugin = WikiRecallPlugin()
        await plugin.before_run_callback(invocation_context=ictx)
        await plugin.after_run_callback(invocation_context=ictx)
        assert _FakeLlm.calls == 0, "auto-capture must not run without the flag"
    asyncio.run(run())
    print("OK autocapture_disabled_by_default")


def test_autocapture_writes_inbox():
    async def run():
        with tempfile.TemporaryDirectory() as root:
            os.environ["ADK_CC_WIKI_ROOT"] = root
            os.environ["ADK_CC_WIKI_AUTOCAPTURE"] = "1"
            _FakeLlm.calls = 0
            ictx = await _make_ictx()
            plugin = WikiRecallPlugin()
            await plugin.before_run_callback(invocation_context=ictx)
            await plugin.after_run_callback(invocation_context=ictx)
            assert _FakeLlm.calls == 1
            inbox = WikiStore.for_tenant("acme").list_inbox("alice")
            assert len(inbox) == 1, inbox
            assert inbox[0].slug == "api-rate-limit"
            assert "1000" in inbox[0].page.body
    asyncio.run(run())
    os.environ.pop("ADK_CC_WIKI_AUTOCAPTURE", None)
    print("OK autocapture_writes_inbox")


def test_autocapture_none_writes_nothing():
    async def run():
        with tempfile.TemporaryDirectory() as root:
            os.environ["ADK_CC_WIKI_ROOT"] = root
            os.environ["ADK_CC_WIKI_AUTOCAPTURE"] = "1"
            _FakeLlm.calls = 0
            ictx = await _make_ictx(
                user_text="what is our rate limit?",
                llm=_FakeLlm(model="fake/model", reply="NONE"),
            )
            plugin = WikiRecallPlugin()
            await plugin.before_run_callback(invocation_context=ictx)
            await plugin.after_run_callback(invocation_context=ictx)
            assert WikiStore.for_tenant("acme").list_inbox("alice") == []
    asyncio.run(run())
    os.environ.pop("ADK_CC_WIKI_AUTOCAPTURE", None)
    print("OK autocapture_none_writes_nothing")


def test_autocapture_overlaps_turn():
    async def run():
        with tempfile.TemporaryDirectory() as root:
            os.environ["ADK_CC_WIKI_ROOT"] = root
            os.environ["ADK_CC_WIKI_AUTOCAPTURE"] = "1"
            ictx = await _make_ictx(llm=_FakeLlm(model="fake/model", delay=0.3))
            plugin = WikiRecallPlugin()
            t0 = time.perf_counter()
            await plugin.before_run_callback(invocation_context=ictx)  # spawn
            await asyncio.sleep(0.3)                                   # the "turn"
            await plugin.after_run_callback(invocation_context=ictx)   # persist
            elapsed = time.perf_counter() - t0
            assert WikiStore.for_tenant("acme").list_inbox("alice"), "should capture"
            assert elapsed < 0.5, f"not overlapped: {elapsed:.3f}s (~0.6s = serial)"
            print(f"  (0.3s extract + 0.3s turn = {elapsed:.3f}s — overlapped)")
    asyncio.run(run())
    os.environ.pop("ADK_CC_WIKI_AUTOCAPTURE", None)
    print("OK autocapture_overlaps_turn")


def test_autocapture_failure_never_breaks_run():
    async def run():
        with tempfile.TemporaryDirectory() as root:
            os.environ["ADK_CC_WIKI_ROOT"] = root
            os.environ["ADK_CC_WIKI_AUTOCAPTURE"] = "1"
            ictx = await _make_ictx(llm=_FakeLlm(model="fake/model", explode=True))
            plugin = WikiRecallPlugin()
            await plugin.before_run_callback(invocation_context=ictx)
            await plugin.after_run_callback(invocation_context=ictx)  # must not raise
            assert WikiStore.for_tenant("acme").list_inbox("alice") == []
    asyncio.run(run())
    os.environ.pop("ADK_CC_WIKI_AUTOCAPTURE", None)
    print("OK autocapture_failure_never_breaks_run")


def test_parse_capture():
    assert _parse_capture("NONE") is None
    assert _parse_capture("  none  ") is None
    assert _parse_capture("") is None
    assert _parse_capture("TOPIC: X\n") is None  # no fact
    t, f = _parse_capture("TOPIC: Rate Limit\nThe API allows 1000 req/min.")
    assert t == "Rate Limit" and "1000" in f
    # multi-line fact joins
    t2, f2 = _parse_capture("TOPIC: Spec\nLine one.\nLine two.")
    assert t2 == "Spec" and "Line one. Line two." == f2
    print("OK parse_capture")


def main():
    test_recall_injects_relevant_block()
    test_recall_surfaces_discrepancy()
    test_recall_skips_empty_query_and_missing_wiki()
    test_recall_preserves_existing_system_instruction()
    test_autocapture_disabled_by_default()
    test_autocapture_writes_inbox()
    test_autocapture_none_writes_nothing()
    test_autocapture_overlaps_turn()
    test_autocapture_failure_never_breaks_run()
    test_parse_capture()
    print("\nall wiki-recall tests passed")


if __name__ == "__main__":
    main()
