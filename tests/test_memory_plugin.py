"""Tests for MemoryPlugin (plugins/memory.py).

Recall injection (before_model, no model call) + full-turn capture
(after_run, fake BaseLlm) that reads user + agent + tool events. Hand-rolled.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace
from typing import AsyncGenerator

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from adk_cc.memory import MemoryItem, MemoryStore
from adk_cc.plugins.memory import MemoryPlugin, _parse_facts


def _tenant_state(tenant="acme", user="alice"):
    return {"temp:tenant_context": SimpleNamespace(tenant_id=tenant, user_id=user)}


def _user_req(text):
    return LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=text)])],
        config=types.GenerateContentConfig(),
    )


def _si(req):
    si = req.config.system_instruction
    if si is None:
        return ""
    return si if isinstance(si, str) else " ".join(
        getattr(p, "text", "") or "" for p in (si if isinstance(si, list) else [si])
    )


# ---------------- recall (no model) ----------------
def test_recall_injects_known_facts():
    with tempfile.TemporaryDirectory() as root:
        os.environ["ADK_CC_MEMORY_ROOT"] = root
        st = MemoryStore.for_tenant("acme")
        st.put_semantic("alice", MemoryItem(id="name", topic="name",
                        text="The user's name is Jisu.", confidence=0.9))
        plugin = MemoryPlugin()
        req = _user_req("what's my name again?")
        cc = SimpleNamespace(state=_tenant_state(), session=None)
        asyncio.run(plugin.before_model_callback(callback_context=cc, llm_request=req))
        si = _si(req)
        assert "Memory (recalled" in si and "Jisu" in si, si[:160]
    os.environ.pop("ADK_CC_MEMORY_ROOT", None)
    print("OK recall_injects_known_facts")


def test_recall_skips_empty_query():
    with tempfile.TemporaryDirectory() as root:
        os.environ["ADK_CC_MEMORY_ROOT"] = root
        plugin = MemoryPlugin()
        req = LlmRequest(
            contents=[types.Content(role="model", parts=[types.Part(text="hi")])],
            config=types.GenerateContentConfig(),
        )
        cc = SimpleNamespace(state=_tenant_state(), session=None)
        asyncio.run(plugin.before_model_callback(callback_context=cc, llm_request=req))
        assert _si(req) == ""
    os.environ.pop("ADK_CC_MEMORY_ROOT", None)
    print("OK recall_skips_empty_query")


# ---------------- capture (fake model) ----------------
class _FakeLlm(BaseLlm):
    reply: str = (
        "TOPIC: deploy target | The project deploys to Fly.io.\n"
        "TOPIC: db choice | The team chose Postgres over MySQL."
    )
    explode: bool = False
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False) -> AsyncGenerator[LlmResponse, None]:
        type(self).calls += 1
        if self.explode:
            raise RuntimeError("model down")
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=self.reply)]))


async def _make_ictx(llm=None):
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="t", user_id="u", state={})
    session.state["temp:tenant_context"] = SimpleNamespace(tenant_id="acme", user_id="alice")
    # seed a full turn: user msg + agent text + tool result, same invocation
    inv = "inv-run"
    def _ev(author, parts):
        return Event(invocation_id=inv, author=author,
                     content=types.Content(role="user" if author == "user" else "model", parts=parts))
    await svc.append_event(session, _ev("user", [types.Part(text="deploy the app and pick a db")]))
    await svc.append_event(session, _ev("coordinator", [types.Part(text="Deployed to Fly.io; chose Postgres.")]))
    agent = LlmAgent(name="t", model=llm or _FakeLlm(model="fake/model"))
    return InvocationContext(session_service=svc, invocation_id=inv, agent=agent,
                             session=session, user_content=None)


def test_capture_writes_episodic_from_full_turn():
    async def run():
        with tempfile.TemporaryDirectory() as root:
            os.environ["ADK_CC_MEMORY_ROOT"] = root
            os.environ.pop("ADK_CC_MEMORY_AUTOCAPTURE", None)
            _FakeLlm.calls = 0
            ictx = await _make_ictx()
            await MemoryPlugin().after_run_callback(invocation_context=ictx)
            assert _FakeLlm.calls == 1
            epi = MemoryStore.for_tenant("acme").list_episodic("alice")
            topics = {i.topic for i in epi}
            assert "deploy-target" in topics and "db-choice" in topics, topics
        os.environ.pop("ADK_CC_MEMORY_ROOT", None)
    asyncio.run(run())
    print("OK capture_writes_episodic_from_full_turn")


def test_capture_disabled_via_env():
    async def run():
        with tempfile.TemporaryDirectory() as root:
            os.environ["ADK_CC_MEMORY_ROOT"] = root
            os.environ["ADK_CC_MEMORY_AUTOCAPTURE"] = "0"
            _FakeLlm.calls = 0
            ictx = await _make_ictx()
            await MemoryPlugin().after_run_callback(invocation_context=ictx)
            assert _FakeLlm.calls == 0
            assert MemoryStore.for_tenant("acme").list_episodic("alice") == []
        os.environ.pop("ADK_CC_MEMORY_AUTOCAPTURE", None)
        os.environ.pop("ADK_CC_MEMORY_ROOT", None)
    asyncio.run(run())
    print("OK capture_disabled_via_env")


def test_capture_failure_never_breaks_run():
    async def run():
        with tempfile.TemporaryDirectory() as root:
            os.environ["ADK_CC_MEMORY_ROOT"] = root
            ictx = await _make_ictx(llm=_FakeLlm(model="fake/model", explode=True))
            await MemoryPlugin().after_run_callback(invocation_context=ictx)  # must not raise
            assert MemoryStore.for_tenant("acme").list_episodic("alice") == []
        os.environ.pop("ADK_CC_MEMORY_ROOT", None)
    asyncio.run(run())
    print("OK capture_failure_never_breaks_run")


def test_parse_facts():
    assert _parse_facts("NONE") == []
    assert _parse_facts("") == []
    facts = _parse_facts("TOPIC: A | first\nTOPIC: B | second\ngarbage line")
    assert facts == [("A", "first"), ("B", "second")]
    assert _parse_facts("TOPIC: only topic no pipe") == []
    print("OK parse_facts")


def main():
    test_recall_injects_known_facts()
    test_recall_skips_empty_query()
    test_capture_writes_episodic_from_full_turn()
    test_capture_disabled_via_env()
    test_capture_failure_never_breaks_run()
    test_parse_facts()
    print("\nall memory-plugin tests passed")


if __name__ == "__main__":
    main()
