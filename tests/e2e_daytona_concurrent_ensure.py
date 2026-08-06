"""Two concurrent ensure_workspace calls must not freeze the event loop.

THE production freeze (#116): the customer reported the whole web server
hanging — not one request, the server. Single skill loads were fine; loading
TWO hung it. A SIGUSR1 dump landed on daytona_backend.ensure_workspace, and
the cause was a threading.Lock held across the create POST and the
started-poll. Tool call A took the lock and awaited; call B on the SAME loop
called .acquire(), which blocks the OS thread that IS the loop. A could never
resume. Fixed with a per-event-loop asyncio.Lock.

Two things this test must do, both learned by getting them wrong first:
  * the mock must report "creating" so ensure_workspace POLLS — the
    asyncio.sleep inside the critical section is the yield that lets the
    second caller reach the lock. An instantly-"started" mock never suspends
    and the first version of this test passed against the buggy code.
  * assert the LOOP stays live (heartbeats), not merely that the calls
    return. Against the old lock this file does not fail, it HANGS FOREVER —
    a blocked loop cannot fire asyncio.wait_for's own timer, which is exactly
    why the outage looked like a dead server.

Run: PYTHONPATH=agents .venv/bin/python tests/e2e_daytona_concurrent_ensure.py
"""
import asyncio, os, sys, time
sys.path.insert(0, "agents")
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
import httpx
from adk_cc.sandbox.backends.daytona_backend import DaytonaBackend
from adk_cc.sandbox.workspace import WorkspaceRoot

async def main():
    calls = {"create": 0}

    async def handler(request):
        # A SLOW create: this is what the old lock was held across.
        if request.url.path.endswith("/sandbox") and request.method == "POST":
            calls["create"] += 1
            await asyncio.sleep(0.6)
            return httpx.Response(200, json={"id": "sb-1", "state": "started"})
        return httpx.Response(200, json={"id": "sb-1", "state": "started"})

    # The sandbox must report "creating" first so ensure_workspace POLLS —
    # asyncio.sleep inside the critical section is what YIELDS the loop, and
    # only then can a second caller reach the lock. An instantly-"started"
    # mock never suspends, so it cannot reproduce the deadlock at all (the
    # first version of this test passed against the buggy code).
    state = {"polls": 0}

    def route(r):
        if r.method == "POST":
            calls["create"] += 1
            return httpx.Response(200, json={"id": "sb-1", "state": "creating"})
        state["polls"] += 1
        st = "started" if state["polls"] > 3 else "creating"
        return httpx.Response(200, json={"id": "sb-1", "state": st})

    client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    be = DaytonaBackend(session_id="s", tenant_id="t",
                        api_url="https://api.test", proxy_url="https://px.test",
                        api_key="k", client=client)
    ws = WorkspaceRoot(tenant_id="t", session_id="s", abs_path="/home/daytona")

    # The heartbeat proves the LOOP is alive while both calls are in flight.
    beats = {"n": 0}
    async def heartbeat():
        for _ in range(40):
            await asyncio.sleep(0.05)
            beats["n"] += 1

    t = time.perf_counter()
    try:
        await asyncio.wait_for(
            asyncio.gather(be.ensure_workspace(ws), be.ensure_workspace(ws),
                           heartbeat()),
            timeout=15)
    except asyncio.TimeoutError:
        print("FAIL: deadlocked — the loop never came back")
        return 1
    print(f"two concurrent ensure_workspace: ok in {time.perf_counter()-t:.2f}s")
    print(f"loop stayed responsive: {beats['n']} heartbeats during the calls")
    print("PASS" if beats["n"] > 5 else "FAIL: loop was blocked")
    return 0 if beats["n"] > 5 else 1

sys.exit(asyncio.run(main()))
