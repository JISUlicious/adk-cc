"""LIVE proof: does verification's SECOND run in one turn do real work?

Coordinator is scripted (so the re-entry is deterministic); the verification
branch goes to the REAL configured model. Between run 1 and run 2 the harness
repairs the defect on disk, so run 2 has something true to find.

Arms: fix OFF (marker leaks into the request) vs ON. Paced for the rate limit.
"""
import asyncio, os, pathlib, shutil, sys, time

WS = pathlib.Path("/tmp/reentry-live")
os.environ.setdefault("ADK_CC_DESKTOP", "1")
os.environ.setdefault("ADK_CC_DESKTOP_DATA", "/tmp/reentry-live-data")
os.environ["ADK_CC_WORKSPACE_ROOT"] = str(WS)
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ["ADK_CC_SANDBOX_BACKEND"] = "noop"   # no cloud sandboxes for a probe

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

BROKEN = "<html><body><div id='game'></div><script src='game.js'></script></body></html>"
FIXED = "<html><body><canvas id='board' width='320' height='640'></canvas><script src='game.js'></script></body></html>"


def reset(broken=True):
    shutil.rmtree(WS, ignore_errors=True)
    WS.mkdir(parents=True)
    (WS / "index.html").write_text(BROKEN if broken else FIXED)
    (WS / "game.js").write_text("const c = document.getElementById('board');\n")


def text(t):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=t)]), partial=False)


def call(cid, name, args):
    return LlmResponse(content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(id=cid, name=name, args=args))]), partial=False)


class Router(BaseLlm):
    """Verification -> real model; everything else -> scripted."""
    model: str = "router"
    real: object = None
    scripted: list = Field(default_factory=list)
    runs: list = Field(default_factory=list)   # per verification run: (tool_calls, text)
    _in_verif: bool = False

    async def generate_content_async(self, req, stream: bool = False):
        si = str(req.config.system_instruction or "")
        low = si.lower()
        if "verification specialist" in low:
            who = "verification"
        elif "you are the coordinator" in low:
            who = "coordinator"
        else:
            # Title/memory plugins share MODEL; they must NOT eat the queue.
            who = "side"
        if who == "side":
            yield text("side")
            return
        print(f"    · model call -> {who} ({len(req.contents or [])} contents) si={si[:52]!r}", flush=True)
        if who == "verification":
            # SelectableLlm stamps llm_request.model from ITS delegate (= this
            # router); LiteLlm then reads `req.model or self.model` and chokes
            # on "router". Hand the real delegate its own id back.
            if getattr(self.real, "model", None) and hasattr(req, "model"):
                req.model = self.real.model
            first = not self._in_verif
            self._in_verif = True
            if first:
                self.runs.append({"calls": [], "text": "", "empty_responses": 0})
            async for r in self.real.generate_content_async(req, stream=False):
                c = getattr(r, "content", None)
                parts = list(getattr(c, "parts", None) or []) if c else []
                if not parts:
                    self.runs[-1]["empty_responses"] += 1
                for p in parts:
                    if getattr(p, "function_call", None):
                        self.runs[-1]["calls"].append(p.function_call.name)
                    elif getattr(p, "text", None) and not getattr(p, "thought", False):
                        self.runs[-1]["text"] += p.text
                yield r
            return
        self._in_verif = False
        if not self.scripted:
            yield text("(coordinator: done)")
            return
        item = self.scripted.pop(0)
        if item == "REPAIR":                       # coordinator "fixes" the bug
            (WS / "index.html").write_text(FIXED)
            item = self.scripted.pop(0)
        yield item


async def arm(label, fix):
    reset()
    import adk_cc.agent as A
    from adk_cc.plugins.handback_hygiene import HandbackHygienePlugin
    from google.adk.plugins.base_plugin import BasePlugin
    HandbackHygienePlugin.before_model_callback = _REAL if fix else BasePlugin.before_model_callback

    real = A.MODEL._resolve_delegate()
    router = Router(real=real, scripted=[
        call("t1", "transfer_to_agent", {"agent_name": "verification"}),
        "REPAIR",
        call("t2", "transfer_to_agent", {"agent_name": "verification"}),
        text("FINAL: reported"),
    ])
    A.MODEL._resolve_delegate = lambda: router
    svc = InMemorySessionService()
    runner = Runner(app=A.app, session_service=svc, artifact_service=None, memory_service=None)
    s = await svc.create_session(app_name=A.app.name, user_id="u",
                                 state={"permission_mode": "bypassPermissions"})
    try:
        await asyncio.wait_for(_drain(runner, s.id), 600)
    except Exception as e:  # noqa: BLE001
        print(f"  [{label}] run error: {type(e).__name__}: {str(e)[:160]}")
    print(f"\n  ARM {label}")
    for i, r in enumerate(router.runs, 1):
        body = r["text"].strip().replace("\n", " ")[:90]
        print(f"    verification run {i}: {len(r['calls'])} tool call(s) "
              f"{r['calls'][:6]}, empty_responses={r['empty_responses']}, "
              f"text={body!r}")
    if len(router.runs) < 2:
        print("    (re-entry never happened — harness problem, not a result)")
    return router.runs


async def _drain(runner, sid):
    async for _ in runner.run_async(user_id="u", session_id=sid,
                                    new_message=types.Content(
                                        role="user",
                                        parts=[types.Part(text="Verify index.html renders a canvas board.")])):
        pass


from adk_cc.plugins.handback_hygiene import HandbackHygienePlugin  # noqa: E402
_REAL = HandbackHygienePlugin.before_model_callback


async def main():
    for label, fix in (("fix OFF", False), ("fix ON", True)):
        await arm(label, fix)
        time.sleep(10)   # pace the rate-limited endpoint


asyncio.run(main())
