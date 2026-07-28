"""Side-by-side trace of every model call in a re-entry turn, fix OFF vs ON.

Runs the REAL adk-cc app (coordinator + verification + both plugins) against a
scripted LLM, and prints the `contents` each model call receives.
"""
import asyncio, os, sys, tempfile
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy")
_T = tempfile.mkdtemp(prefix="trace-")
os.environ.setdefault("ADK_CC_DESKTOP", "1")
os.environ.setdefault("ADK_CC_DESKTOP_DATA", _T)
os.environ.setdefault("ADK_CC_WORKSPACE_ROOT", _T)

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

HB = "_handback_to_coordinator"


def text(t):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=t)]), partial=False)


def call(cid, name, args):
    return LlmResponse(content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(id=cid, name=name, args=args))]), partial=False)


def describe(contents):
    """role: part-kinds, plus which call ids are left unanswered."""
    rows, open_calls = [], {}
    for c in contents or []:
        kinds = []
        for p in (c.parts or []):
            fc, fr = getattr(p, "function_call", None), getattr(p, "function_response", None)
            if fc:
                kinds.append(f"CALL {fc.name}#{(fc.id or 'NO-ID')[:12]}")
                open_calls[fc.id] = len(rows)
            elif fr:
                kinds.append(f"RESP {fr.name}#{(fr.id or 'NO-ID')[:12]}")
                open_calls.pop(fr.id, None)
            elif getattr(p, "text", None):
                snippet = p.text.strip().replace("\n", " ")[:38]
                kinds.append(f'text({len(p.text)}) "{snippet}"')
        rows.append([c.role, ", ".join(kinds) or "(empty)", ""])
    for idx in open_calls.values():
        rows[idx][2] = "  <== UNANSWERED"
    return rows


class Tracer(BaseLlm):
    model: str = "fake/trace"
    responses: list = Field(default_factory=list)
    captures: list = Field(default_factory=list)

    async def generate_content_async(self, req, stream: bool = False):
        si = str(req.config.system_instruction or "")
        who = "verification" if "verification specialist" in si.lower() else "coordinator"
        self.captures.append((who, describe(req.contents)))
        if not self.responses:
            yield text("(exhausted)")
            return
        yield self.responses.pop(0)


async def arm(label: str, fix: bool):
    import adk_cc.agent as A
    from adk_cc.plugins.handback_hygiene import HandbackHygienePlugin
    from google.adk.plugins.base_plugin import BasePlugin

    HandbackHygienePlugin.before_model_callback = (
        _REAL_BEFORE if fix else BasePlugin.before_model_callback
    )
    llm = Tracer(responses=[
        call("t1", "transfer_to_agent", {"agent_name": "verification"}),   # #1 coordinator
        call("g1", "glob_files", {"pattern": "*.py"}),                     # #2 verification run 1
        text("VERDICT: FAIL — index.html has no <canvas>"),                # #3 verification run 1
        call("t2", "transfer_to_agent", {"agent_name": "verification"}),   # #4 coordinator re-entry
        call("g2", "glob_files", {"pattern": "*.html"}),                   # #5 verification run 2
        text("VERDICT: PASS — canvas present"),                            # #6 verification run 2
        text("FINAL: fixed and verified"),                                 # #7 coordinator
    ])
    A.MODEL._resolve_delegate = lambda: llm
    svc = InMemorySessionService()
    runner = Runner(app=A.app, session_service=svc, artifact_service=None, memory_service=None)
    s = await svc.create_session(app_name=A.app.name, user_id="u")
    async for _ in runner.run_async(user_id="u", session_id=s.id,
                                    new_message=types.Content(role="user", parts=[types.Part(text="check it")])):
        pass

    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    for i, (who, rows) in enumerate(llm.captures, 1):
        tag = "  <-- RE-ENTRY" if who == "verification" and i > 3 else ""
        print(f"\n  model call #{i}  [{who}] — {len(rows)} contents{tag}")
        for role, kinds, note in rows:
            print(f"      {role:<6} {kinds}{note}")
    sess = await svc.get_session(app_name=A.app.name, user_id="u", session_id=s.id)
    hb_calls = sum(1 for e in sess.events for p in (getattr(e.content, "parts", None) or [])
                   if getattr(p, "function_call", None) and p.function_call.name == HB)
    hb_resps = sum(1 for e in sess.events for p in (getattr(e.content, "parts", None) or [])
                   if getattr(p, "function_response", None) and p.function_response.name == HB)
    print(f"\n  persisted history: {hb_calls} handback CALL(s) (broker needs these), "
          f"{hb_resps} handback RESP(s)")


from adk_cc.plugins.handback_hygiene import HandbackHygienePlugin  # noqa: E402
_REAL_BEFORE = HandbackHygienePlugin.before_model_callback


async def main():
    await arm("BEFORE the fix — history leaks the marker into the request", fix=False)
    await arm("AFTER the fix — request-side strip", fix=True)


asyncio.run(main())
