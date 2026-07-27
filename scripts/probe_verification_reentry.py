"""Re-entry against the REAL adk-cc app (handback + hygiene plugins active)."""
import asyncio, os, tempfile
os.environ.setdefault("ADK_CC_SKIP_DOTENV","1"); os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK","1")
os.environ.setdefault("ADK_CC_API_KEY","sk-dummy")
_T=tempfile.mkdtemp(prefix="reentry-"); os.environ.setdefault("ADK_CC_DESKTOP","1")
os.environ.setdefault("ADK_CC_DESKTOP_DATA",_T); os.environ.setdefault("ADK_CC_WORKSPACE_ROOT",_T)

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

def text(t): return LlmResponse(content=types.Content(role="model",parts=[types.Part(text=t)]),partial=False)
def call(cid,name,args): return LlmResponse(content=types.Content(role="model",
    parts=[types.Part(function_call=types.FunctionCall(id=cid,name=name,args=args))]),partial=False)

class Scripted(BaseLlm):
    model: str = "fake/reentry"
    responses: list = Field(default_factory=list)
    calls: int = 0
    async def generate_content_async(self, req, stream: bool=False):
        self.calls += 1
        if not self.responses:
            yield text("(exhausted)"); return
        yield self.responses.pop(0)

async def run(fix: bool):
    import adk_cc.agent as A
    llm = Scripted(responses=[
        call("t1","transfer_to_agent",{"agent_name":"verification"}),
        call("g1","glob_files",{"pattern":"*.py"}),      # verification does real work
        call("b1","run_bash",{"command":"echo probe"}),
        text("VERDICT: FAIL — first pass"),
        call("t2","transfer_to_agent",{"agent_name":"verification"}),
        text("VERDICT: PASS — second pass"),
        text("FINAL ANSWER"),
    ])
    A.MODEL._resolve_delegate = lambda: llm
    if fix:
        os.environ["ADK_CC_VERIFY_RERUN"] = "1"
    else:
        os.environ.pop("ADK_CC_VERIFY_RERUN", None)
    import importlib
    svc = InMemorySessionService()
    runner = Runner(app=A.app, session_service=svc, artifact_service=None, memory_service=None)
    s = await svc.create_session(app_name=A.app.name, user_id="u")
    runs=[]; in_run=False; verdicts=[]
    async for ev in runner.run_async(user_id="u", session_id=s.id,
            new_message=types.Content(role="user",parts=[types.Part(text="check it")])):
        a = ev.author
        if a=="verification":
            if not in_run: runs.append(0); in_run=True
        else: in_run=False
        for p in (ev.content.parts if ev.content else []) or []:
            if getattr(p,"function_call",None) and a=="verification" and p.function_call.name!="_handback_to_coordinator":
                runs[-1]+=1
            t=getattr(p,"text",None)
            if t and a=="verification" and "VERDICT" in t: verdicts.append(t.strip()[:34])
    print(f"  fix={fix}: verification runs={runs} verdicts={verdicts} model_calls={llm.calls}")

asyncio.run(run(False))
