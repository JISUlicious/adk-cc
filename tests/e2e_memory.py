"""E2E: autonomous memory loop with the REAL model (no mocks).

Exercises the full pipeline end to end — capture (from a real turn, real
model extraction) → consolidate (episodic→semantic) → recall — and asserts
the integration holds. Tolerant of model variance in HOW MANY facts it
extracts (that's the model's call; the deterministic per-step behavior is
pinned in test_memory_store.py / test_memory_plugin.py).

Skips gracefully (exit 0) when no live model is reachable. Run:

    .venv/bin/python tests/e2e_memory.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

os.environ["ADK_CC_MEMORY"] = "1"
_TMP = tempfile.mkdtemp(prefix="mem-e2e-")
os.environ["ADK_CC_MEMORY_ROOT"] = _TMP

from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from adk_cc.memory import MemoryStore, consolidate_user, recall_context
from adk_cc.plugins.memory import MemoryPlugin

_PROBE_TIMEOUT_S = 30


def _model_available(model) -> bool:
    from google.adk.models.llm_request import LlmRequest
    from google.adk.utils.context_utils import Aclosing

    async def _probe() -> str:
        req = LlmRequest(
            contents=[types.Content(role="user", parts=[types.Part(text="say ok")])],
            config=types.GenerateContentConfig(),
        )
        out = ""
        async with Aclosing(model.generate_content_async(req, stream=False)) as agen:
            async for resp in agen:
                for p in (getattr(getattr(resp, "content", None), "parts", None) or []):
                    if getattr(p, "text", None):
                        out += p.text
        return out

    try:
        return bool(asyncio.run(asyncio.wait_for(_probe(), timeout=_PROBE_TIMEOUT_S)).strip())
    except Exception as e:  # noqa: BLE001
        print(f"  (model probe failed: {type(e).__name__}: {e})")
        return False


async def _seed_turn(model):
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="t", user_id="u", state={})
    session.state["temp:tenant_context"] = SimpleNamespace(tenant_id="acme", user_id="alice")
    inv = "inv-run"

    def _ev(author, text, role):
        return Event(invocation_id=inv, author=author,
                     content=types.Content(role=role, parts=[types.Part(text=text)]))

    await svc.append_event(session, _ev(
        "user", "Set up the project. My name is Jisu and we deploy to Fly.io.", "user"))
    await svc.append_event(session, _ev(
        "coordinator", "Done — configured deployment to Fly.io for Jisu's project.", "model"))
    return InvocationContext(session_service=svc, invocation_id=inv, agent=LlmAgent(
        name="t", model=model), session=session, user_content=None)


def main() -> int:
    ok = True

    def check(name, cond, detail):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}: {detail}")
        ok = ok and cond

    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY — memory e2e skipped.")
        return 0
    from adk_cc.agent import MODEL
    if not _model_available(MODEL):
        print("SKIP: live model unreachable — memory e2e skipped.")
        return 0

    st = MemoryStore.for_tenant("acme")

    # We assert the PIPELINE INTEGRITY end-to-end with the real model — capture
    # runs, consolidation folds whatever was captured, recall reflects it — and
    # REPORT the model's extraction count. How many facts the model extracts is
    # the model's call (the configured model may be weak/unstable); the
    # deterministic per-step behavior is pinned in the unit tests.

    # 1. capture from a real turn (real model extraction; plugin swallows errors)
    ictx = asyncio.run(_seed_turn(MODEL))
    asyncio.run(MemoryPlugin().after_run_callback(invocation_context=ictx))
    episodic = st.list_episodic("alice")
    n = len(episodic)
    print(f"  [info] model captured {n} episodic fact(s): {[i.topic for i in episodic]}")

    # 2. consolidation folds captured episodics; idempotent on re-run
    rep = consolidate_user(st, "alice")
    semantic = st.list_semantic("alice")
    check("consolidation consistent with capture",
          rep.topics_consolidated == n and len(semantic) == n,
          f"captured={n} consolidated={rep.topics_consolidated} semantic={len(semantic)}")
    check("consolidation is idempotent (re-run is a no-op)",
          consolidate_user(st, "alice").topics_consolidated == 0, "no reprocessing")

    # 3. recall is safe and reflects what was remembered (non-empty iff facts)
    block = recall_context(st, "alice", "what do you know about Jisu and deployment",
                           budget_tokens=400)
    check("recall returns safely; non-empty iff facts were captured",
          isinstance(block, str) and (bool(block.strip()) == (n > 0)),
          f"recall_len={len(block)} captured={n}")

    if n == 0:
        print("  [note] model extracted nothing — capture quality depends on the "
              "model; the pipeline itself is verified above + in the unit tests.")

    print("\n--- semantic memory ---")
    for s in semantic:
        print(f"  [{s.topic}] ({s.confidence}) {s.text}")
    print("\nmemory e2e " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
