"""Proves the concurrency fixes keep the event loop free.

A probe task ticks every 5ms and records the largest gap between ticks while an
operation is in flight. If the operation blocks the loop, the probe can't tick
and the gap balloons; if it's offloaded (asyncio.to_thread / fire-and-forget),
the probe keeps ticking and the gap stays tiny.

The CONTROL test deliberately blocks the loop to prove the probe actually
detects stalls — otherwise "the loop was free" could be a false pass. Then the
real fixed paths (login scrypt, web_fetch) must keep the loop free.

Model-free. Run: .venv/bin/python tests/test_event_loop_freedom.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
os.environ["ADK_CC_WEB_FETCH_ALLOW_PRIVATE"] = "1"  # allow the local test server

# A loop-blocked tick can't exceed this; an offloaded op should keep ticks tiny.
_FREE = 0.10    # loop stayed free → max gap well under 100ms
_STALLED = 0.20  # loop was blocked by the 0.30s op → gap well over 200ms
_BLOCK_S = 0.30


class _Probe:
    """Async context manager that measures loop responsiveness around its body."""

    def __init__(self) -> None:
        self.gaps: list[float] = []
        self._running = False
        self._task: asyncio.Future | None = None

    async def _tick(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while self._running:
            await asyncio.sleep(0.005)
            now = loop.time()
            self.gaps.append(now - last)
            last = now

    async def __aenter__(self) -> "_Probe":
        self._running = True
        self._task = asyncio.ensure_future(self._tick())
        await asyncio.sleep(0.03)  # warm up
        return self

    async def __aexit__(self, *exc) -> None:
        await asyncio.sleep(0.02)
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def max_gap(self) -> float:
        return max(self.gaps) if self.gaps else 0.0


# ---------- control: prove the probe detects a real loop stall ----------
def test_control_onloop_block_is_detected():
    async def run() -> float:
        async with _Probe() as p:
            time.sleep(_BLOCK_S)  # BLOCK the event loop synchronously
        return p.max_gap

    gap = asyncio.run(run())
    assert gap > _STALLED, f"probe failed to detect an on-loop block (max gap {gap:.3f}s)"


def test_to_thread_keeps_loop_free():
    async def run() -> float:
        async with _Probe() as p:
            await asyncio.to_thread(time.sleep, _BLOCK_S)  # same block, OFF-loop
        return p.max_gap

    gap = asyncio.run(run())
    assert gap < _FREE, f"to_thread still stalled the loop (max gap {gap:.3f}s)"


# ---------- real path: login scrypt is offloaded ----------
def test_login_scrypt_does_not_block_loop():
    from adk_cc.identity.provider import EmailPasswordProvider
    from adk_cc.identity.store import JsonFileUserStore

    d = tempfile.mkdtemp(prefix="loopfree-")
    store = JsonFileUserStore(os.path.join(d, "users.json"))
    p = EmailPasswordProvider(store, mode="single", global_tenant_id="acme")
    p.provision(email="a@x.io", password="password123", tenant_id="acme", roles=["admin"])

    async def run():
        async with _Probe() as probe:
            # several concurrent logins (scrypt is ~16 MiB memory-hard each)
            results = await asyncio.gather(
                *[p.login_password("a@x.io", "password123") for _ in range(6)]
            )
        return probe.max_gap, results

    gap, results = asyncio.run(run())
    assert all(r is not None for r in results), "login should succeed"
    assert gap < _FREE, f"login scrypt stalled the loop (max gap {gap:.3f}s)"


# ---------- real path: web_fetch is offloaded ----------
class _SlowHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        time.sleep(_BLOCK_S)  # slow upstream
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"hello from a slow server")

    def log_message(self, *a):  # silence
        pass


def test_web_fetch_does_not_block_loop():
    from adk_cc.tools.schemas import WebFetchArgs
    from adk_cc.tools.web_fetch import WebFetchTool

    srv = HTTPServer(("127.0.0.1", 0), _SlowHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        tool = WebFetchTool()

        async def run():
            async with _Probe() as probe:
                res = await tool._execute(WebFetchArgs(url=f"http://127.0.0.1:{port}/"), None)
            return probe.max_gap, res

        gap, res = asyncio.run(run())
        assert res.get("status") == "ok", f"fetch failed: {res}"
        assert "slow server" in res.get("content", ""), res
        assert gap < _FREE, f"web_fetch stalled the loop (max gap {gap:.3f}s)"
    finally:
        srv.shutdown()


# ---------- real path: SelectableLlm resolve/warm are offloaded ----------
# The first model call builds the LiteLlm delegate, and litellm's cold import is
# heavy (~hundreds of ms) — done on the loop it would stall every concurrent
# request (health checks included) during the first turn. generate_content_async
# and warm() both resolve via asyncio.to_thread; a slow resolve must NOT stall.
class _FakeDelegate:
    async def generate_content_async(self, req, stream=False):  # noqa: D401
        yield "ok"


def test_model_resolve_does_not_block_loop():
    from adk_cc.models.selectable import SelectableLlm

    class _SlowResolveModel(SelectableLlm):
        def _resolve_delegate(self):  # simulate litellm cold-build cost
            time.sleep(_BLOCK_S)
            return _FakeDelegate()

    m = _SlowResolveModel(default_model_id="fake")

    async def run():
        async with _Probe() as probe:
            out = [r async for r in m.generate_content_async(None)]
        return probe.max_gap, out

    gap, out = asyncio.run(run())
    assert out == ["ok"], out
    assert gap < _FREE, f"model resolve stalled the loop (max gap {gap:.3f}s)"


def test_model_warm_keeps_loop_free_and_is_best_effort():
    from adk_cc.models.selectable import SelectableLlm

    class _SlowResolveModel(SelectableLlm):
        def _resolve_delegate(self):
            time.sleep(_BLOCK_S)
            return _FakeDelegate()

    m = _SlowResolveModel(default_model_id="fake")

    async def run():
        async with _Probe() as probe:
            await m.warm()
        return probe.max_gap

    gap = asyncio.run(run())
    assert gap < _FREE, f"warm() stalled the loop (max gap {gap:.3f}s)"

    # best-effort: a config error during warm must never propagate (it would
    # otherwise crash server startup).
    class _BoomModel(SelectableLlm):
        def _resolve_delegate(self):
            raise RuntimeError("no active endpoint")

    asyncio.run(_BoomModel(default_model_id="fake").warm())  # must not raise


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__[5:]}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__[5:]}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__[5:]}: {type(e).__name__}: {e}")
    print("\nall event-loop-freedom tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
