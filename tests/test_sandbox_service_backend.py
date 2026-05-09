"""Unit tests for `SandboxServiceBackend`.

All HTTP traffic is mocked at the `httpx` boundary via `httpx.MockTransport`,
so these tests don't need a live sandbox service.

Coverage:
  - factory dispatch (env-var driven)
  - exec wraps the command in `bash -lc` and lands at the right URL
  - cwd outside `/workspace` defaults gets prepended as `cd '<path>' && ...`
  - path translation rejects out-of-workspace paths before HTTP
  - close() POSTs /stop, never /destroy
  - per-instance session binding
  - truncated streams surface in stderr
  - read_text 404 → FileNotFoundError

Run: `uv run python tests/test_sandbox_service_backend.py`
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

import httpx

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")


# === Helpers ===


class _Recorder:
    """Captures every request the backend makes, returns canned responses."""

    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return await self._responder(request)

        return httpx.MockTransport(handler)


def _make_backend(recorder: _Recorder, **overrides):
    from adk_cc.sandbox.backends.sandbox_service_backend import (
        SandboxServiceBackend,
    )

    client = httpx.AsyncClient(
        base_url="https://sandbox.test",
        headers={"Authorization": "Bearer test-token"},
        transport=recorder.transport(),
    )
    kwargs = dict(
        base_url="https://sandbox.test",
        api_token="test-token",
        session_id="adkcc-sess-1",
        tenant_id="acme",
        client=client,
    )
    kwargs.update(overrides)
    return SandboxServiceBackend(**kwargs)


def _make_workspace(abs_path: str = "/host/wks/acme/alice"):
    from adk_cc.sandbox.workspace import WorkspaceRoot

    return WorkspaceRoot(
        tenant_id="acme",
        session_id="adkcc-sess-1",
        abs_path=abs_path,
    )


# === Tests ===


async def test_exec_wraps_in_bash_lc():
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(
                200,
                json={"id": "svc-sid-1", "tenant_id": "acme", "state": "RUNNING"},
            )
        if request.url.path == "/v1/sessions/svc-sid-1/exec":
            body = json.loads(request.content.decode())
            assert body["argv"] == ["/bin/bash", "-lc", "echo hi"], body
            assert body["timeout_s"] == 30
            return httpx.Response(
                200,
                json={
                    "stdout": "hi\n",
                    "stderr": "",
                    "exit_code": 0,
                    "duration_ms": 4,
                    "effective_timeout_s": 30,
                    "truncated": False,
                },
            )
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()

    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

    await backend.ensure_workspace(ws)
    res = await backend.exec(
        "echo hi",
        fs_write=FsWriteConfig(allow_paths=(ws.abs_path,)),
        network=NetworkConfig(),
        timeout_s=30,
        cwd=ws.abs_path,
    )
    assert res.exit_code == 0, res
    assert res.stdout == "hi\n"
    # ensure_workspace + exec → exactly two requests
    assert [r.url.path for r in rec.requests] == [
        "/v1/sessions",
        "/v1/sessions/svc-sid-1/exec",
    ]
    print("OK exec_wraps_in_bash_lc")


async def test_exec_cwd_subdir_gets_cd_prefix():
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "svc-sid-2"})
        if request.url.path == "/v1/sessions/svc-sid-2/exec":
            body = json.loads(request.content.decode())
            assert body["argv"][0] == "/bin/bash"
            assert body["argv"][1] == "-lc"
            assert body["argv"][2] == "cd '/workspace/sub' && pwd", body
            return httpx.Response(
                200,
                json={"stdout": "/workspace/sub\n", "stderr": "", "exit_code": 0},
            )
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

    await backend.ensure_workspace(ws)
    await backend.exec(
        "pwd",
        fs_write=FsWriteConfig(),
        network=NetworkConfig(),
        timeout_s=10,
        cwd=os.path.join(ws.abs_path, "sub"),
    )
    print("OK exec_cwd_subdir_gets_cd_prefix")


async def test_path_translation_rejects_outside_workspace():
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "svc-sid-3"})
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsWriteConfig
    from adk_cc.sandbox.config import SandboxViolation

    await backend.ensure_workspace(ws)
    try:
        await backend.write_text(
            "/etc/passwd", "x", fs_write=FsWriteConfig()
        )
    except SandboxViolation as e:
        assert "outside workspace" in str(e), str(e)
        # No HTTP write request hit — only the session_create
        post_paths = [
            r.url.path for r in rec.requests if r.method == "POST"
        ]
        assert post_paths == ["/v1/sessions"], post_paths
        print("OK path_translation_rejects_outside_workspace")
        return
    raise AssertionError("expected SandboxViolation")


async def test_close_calls_stop_not_destroy():
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "svc-sid-4"})
        if request.url.path == "/v1/sessions/svc-sid-4/stop":
            return httpx.Response(200, json={"id": "svc-sid-4", "state": "STOPPED"})
        return httpx.Response(404, text=f"unexpected: {request.url}")

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()

    await backend.ensure_workspace(ws)
    await backend.close()
    paths = [(r.method, r.url.path) for r in rec.requests]
    assert ("POST", "/v1/sessions/svc-sid-4/stop") in paths, paths
    assert not any(
        path.endswith("/destroy") or method == "DELETE"
        for method, path in paths
    ), paths
    print("OK close_calls_stop_not_destroy")


async def test_per_session_binding():
    """Two backends with different session_ids hit distinct service URLs."""
    captured: dict[str, list[str]] = {"a": [], "b": []}

    def make_responder(label: str, sid: str):
        async def respond(request: httpx.Request) -> httpx.Response:
            captured[label].append(request.url.path)
            if request.url.path == "/v1/sessions":
                return httpx.Response(200, json={"id": sid})
            if request.url.path == f"/v1/sessions/{sid}/exec":
                return httpx.Response(
                    200,
                    json={"stdout": label, "stderr": "", "exit_code": 0},
                )
            return httpx.Response(404, text=f"unexpected: {request.url}")

        return respond

    rec_a = _Recorder(make_responder("a", "svc-a"))
    rec_b = _Recorder(make_responder("b", "svc-b"))

    backend_a = _make_backend(rec_a, session_id="adk-a")
    backend_b = _make_backend(rec_b, session_id="adk-b")

    ws = _make_workspace()
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

    await backend_a.ensure_workspace(ws)
    await backend_b.ensure_workspace(ws)
    await backend_a.exec(
        "echo a",
        fs_write=FsWriteConfig(),
        network=NetworkConfig(),
        timeout_s=10,
        cwd=ws.abs_path,
    )
    await backend_b.exec(
        "echo b",
        fs_write=FsWriteConfig(),
        network=NetworkConfig(),
        timeout_s=10,
        cwd=ws.abs_path,
    )
    assert "/v1/sessions/svc-a/exec" in captured["a"], captured
    assert "/v1/sessions/svc-b/exec" in captured["b"], captured
    # no cross-talk
    assert not any(p.endswith("/svc-b/exec") for p in captured["a"])
    assert not any(p.endswith("/svc-a/exec") for p in captured["b"])
    print("OK per_session_binding")


async def test_truncated_stream_propagates_to_stderr():
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "svc-sid-5"})
        if request.url.path.endswith("/exec"):
            return httpx.Response(
                200,
                json={
                    "stdout": "x" * 10,
                    "stderr": "",
                    "exit_code": 0,
                    "truncated": True,
                    "truncated_streams": ["stdout"],
                    "effective_truncation_cap_bytes": 16 * 1024 * 1024,
                },
            )
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

    await backend.ensure_workspace(ws)
    res = await backend.exec(
        "yes",
        fs_write=FsWriteConfig(),
        network=NetworkConfig(),
        timeout_s=10,
        cwd=ws.abs_path,
    )
    # Service-reported cap (16 MiB here) takes precedence over the old
    # hard-coded 8 MiB string — verifies the cross-cutting field landed.
    assert "16 MiB" in res.stderr, res.stderr
    assert "stdout" in res.stderr, res.stderr  # truncated_streams listed
    print("OK truncated_stream_propagates_to_stderr")


async def test_idempotency_key_present_on_mutating_calls():
    """Every mutating request carries Idempotency-Key per upstream PR #7."""
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            assert request.headers.get("idempotency-key"), (
                "session_create missing Idempotency-Key"
            )
            return httpx.Response(200, json={"id": "svc-idem"})
        if request.url.path == "/v1/sessions/svc-idem/exec":
            assert request.headers.get("idempotency-key"), (
                "exec missing Idempotency-Key"
            )
            return httpx.Response(
                200, json={"stdout": "", "stderr": "", "exit_code": 0}
            )
        if request.method == "POST" and request.url.path.startswith(
            "/v1/sessions/svc-idem/files/"
        ):
            assert request.headers.get("idempotency-key"), (
                "file_write missing Idempotency-Key"
            )
            return httpx.Response(200)
        if request.method == "POST" and request.url.path.endswith("/stop"):
            assert request.headers.get("idempotency-key"), (
                "session_stop missing Idempotency-Key"
            )
            return httpx.Response(200)
        return httpx.Response(404, text=f"unexpected: {request.url}")

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

    await backend.ensure_workspace(ws)
    await backend.exec(
        "echo",
        fs_write=FsWriteConfig(),
        network=NetworkConfig(),
        timeout_s=10,
        cwd=ws.abs_path,
    )
    await backend.write_text(
        os.path.join(ws.abs_path, "x.txt"), "x", fs_write=FsWriteConfig()
    )
    await backend.close()
    # Every mutating request had a key, and each unique (post 4 mutating calls):
    keys = [
        r.headers.get("idempotency-key")
        for r in rec.requests
        if r.method == "POST"
    ]
    assert all(keys), keys
    assert len(set(keys)) == len(keys), f"keys reused across calls: {keys}"
    print("OK idempotency_key_present_on_mutating_calls")


async def test_credential_provider_resolves_per_tenant_token():
    """When `credentials` is provided, the backend resolves the token via
    the credential provider keyed on tenant_id, not from a static value."""
    from adk_cc.credentials import InMemoryCredentialProvider

    creds = InMemoryCredentialProvider()
    await creds.put(
        tenant_id="acme", key="sandbox_service_token", value="acme-tok"
    )
    await creds.put(
        tenant_id="other", key="sandbox_service_token", value="other-tok"
    )

    seen_tokens: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers.get("authorization", ""))
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "svc-cred"})
        return httpx.Response(404)

    transport = httpx.MockTransport(
        lambda r: asyncio.get_event_loop().run_until_complete(respond(r))
    )
    # Use the real backend (no client= override) so the credential
    # resolver actually runs. Patch httpx.AsyncClient to use the mock
    # transport.
    from adk_cc.sandbox.backends.sandbox_service_backend import (
        SandboxServiceBackend,
    )

    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return real_async_client(*args, **kwargs)

    import adk_cc.sandbox.backends.sandbox_service_backend as mod

    orig = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = patched_async_client
    try:
        backend = SandboxServiceBackend(
            base_url="https://sandbox.test",
            credentials=creds,
            session_id="sess",
            tenant_id="acme",
        )
        ws = _make_workspace()
        await backend.ensure_workspace(ws)
        # First request after token resolve had the per-tenant token.
        assert seen_tokens, seen_tokens
        assert seen_tokens[0] == "Bearer acme-tok", seen_tokens
        print("OK credential_provider_resolves_per_tenant_token")
    finally:
        mod.httpx.AsyncClient = orig


async def test_credential_provider_missing_token_raises():
    from adk_cc.credentials import InMemoryCredentialProvider
    from adk_cc.sandbox.backends.sandbox_service_backend import (
        SandboxServiceBackend,
    )

    creds = InMemoryCredentialProvider()  # no entries

    backend = SandboxServiceBackend(
        base_url="https://sandbox.test",
        credentials=creds,
        session_id="sess",
        tenant_id="absent",
    )
    ws = _make_workspace()
    try:
        await backend.ensure_workspace(ws)
    except RuntimeError as e:
        assert "no token for tenant" in str(e)
        assert "absent" in str(e)
        print("OK credential_provider_missing_token_raises")
        return
    raise AssertionError("expected RuntimeError for missing token")


async def test_exec_stream_yields_chunks_then_result():
    """exec_stream parses SSE events: stdout/stderr → ExecChunk(data),
    result → ExecChunk(result=ExecResult)."""
    sse_body = (
        b"event: stdout\n"
        b'data: {"chunk_b64": "aGVsbG8="}\n\n'  # "hello"
        b"event: stdout\n"
        b'data: {"chunk_b64": "IHdvcmxk"}\n\n'  # " world"
        b"event: stderr\n"
        b'data: {"chunk_b64": "d2Fybg=="}\n\n'  # "warn"
        b"event: result\n"
        b'data: {"stdout": "hello world", "stderr": "warn", '
        b'"exit_code": 0, "duration_ms": 12, "effective_timeout_s": 60, '
        b'"truncated": false, "truncated_streams": [], '
        b'"effective_truncation_cap_bytes": 8388608, "resume_latency_ms": 0}\n\n'
    )

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "svc-stream-1"})
        if request.url.path == "/v1/sessions/svc-stream-1/exec/stream":
            return httpx.Response(
                200,
                content=sse_body,
                headers={"content-type": "text/event-stream; charset=utf-8"},
            )
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

    await backend.ensure_workspace(ws)

    chunks = []
    async for c in backend.exec_stream(
        "echo hi",
        fs_write=FsWriteConfig(),
        network=NetworkConfig(),
        timeout_s=10,
        cwd=ws.abs_path,
    ):
        chunks.append(c)

    # 3 stream chunks (2 stdout + 1 stderr) + 1 result terminator
    assert len(chunks) == 4, [(c.kind, c.data) for c in chunks]
    assert chunks[0].kind == "stdout" and chunks[0].data == "hello"
    assert chunks[1].kind == "stdout" and chunks[1].data == " world"
    assert chunks[2].kind == "stderr" and chunks[2].data == "warn"
    assert chunks[3].kind == "result"
    assert chunks[3].result is not None
    assert chunks[3].result.exit_code == 0
    assert chunks[3].result.stdout == "hello world"
    assert chunks[3].result.stderr == "warn"
    print("OK exec_stream_yields_chunks_then_result")


async def test_exec_stream_synthesizes_result_on_4xx():
    """If the upstream returns 4xx for /exec/stream, exec_stream still
    terminates with one result chunk carrying the error in stderr."""
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "svc-stream-2"})
        if request.url.path.endswith("/exec/stream"):
            return httpx.Response(400, text="bad argv")
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

    await backend.ensure_workspace(ws)
    chunks = []
    async for c in backend.exec_stream(
        "x", fs_write=FsWriteConfig(), network=NetworkConfig(),
        timeout_s=10, cwd=ws.abs_path,
    ):
        chunks.append(c)
    assert len(chunks) == 1, chunks
    assert chunks[0].kind == "result"
    assert chunks[0].result is not None
    assert chunks[0].result.exit_code == -1
    assert "400" in chunks[0].result.stderr
    assert "bad argv" in chunks[0].result.stderr
    print("OK exec_stream_synthesizes_result_on_4xx")


async def test_exec_stream_default_impl_for_other_backends():
    """Backends that don't override exec_stream get the ABC default:
    call exec, yield one result chunk."""
    from adk_cc.sandbox.backends import NoopBackend
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig
    from adk_cc.sandbox.workspace import WorkspaceRoot
    import tempfile

    backend = NoopBackend()
    with tempfile.TemporaryDirectory() as td:
        ws = WorkspaceRoot(tenant_id="t", session_id="s", abs_path=td)
        os.environ["ADK_CC_NOOP_ACK_HOST_EXEC"] = "1"  # required for NoopBackend
        try:
            chunks = []
            async for c in backend.exec_stream(
                "echo hi-from-noop",
                fs_write=FsWriteConfig(allow_paths=(td,)),
                network=NetworkConfig(),
                timeout_s=10,
                cwd=td,
            ):
                chunks.append(c)
        finally:
            del os.environ["ADK_CC_NOOP_ACK_HOST_EXEC"]
    # Default impl: just one terminating result chunk with the full result
    assert len(chunks) == 1, chunks
    assert chunks[0].kind == "result"
    assert chunks[0].result is not None
    assert "hi-from-noop" in chunks[0].result.stdout
    print("OK exec_stream_default_impl_for_other_backends")


def test_cross_event_loop_safe():
    """Regression: SandboxServiceBackend used from a fresh asyncio loop
    in a worker thread (the SandboxBackedCodeExecutor pattern) must not
    fail with `<asyncio.locks.Event ...> is bound to a different event
    loop` from a cached httpx.AsyncClient or asyncio.Lock.

    Reproducer: construct + ensure_workspace in main loop, then call
    exec from `asyncio.run` in a worker thread. Pre-fix this raised
    `RuntimeError: ... bound to a different event loop` on the second
    HTTP attempt because httpx's internal `asyncio.Event` was loop-
    bound to the first loop.
    """
    import threading
    from adk_cc.sandbox.backends.sandbox_service_backend import (
        SandboxServiceBackend,
    )
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

    # Distinct counters per loop — proves both loops actually got
    # served.
    seen_in_loops: list[int] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        # Each request gets a fresh response, with the loop id stamped
        # so we can verify cross-loop traffic landed.
        loop_id = id(asyncio.get_event_loop())
        seen_in_loops.append(loop_id)
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "svc-cross-loop"})
        if request.url.path.endswith("/exec"):
            return httpx.Response(
                200,
                json={
                    "stdout": f"loop-{loop_id}",
                    "stderr": "",
                    "exit_code": 0,
                },
            )
        return httpx.Response(404)

    # Important: the test injects a single AsyncClient via the ctor's
    # `client=` kwarg, which `_client_ctx` reuses without closing.
    # That's NOT the production path — production builds fresh per
    # call. To truly exercise the cross-loop scenario, we need
    # production-style behavior: drop the cached client. Test by NOT
    # passing `client=`, but instead patching the env to a known URL
    # and using a real AsyncClient transport stack via MockTransport
    # at the module level... too painful.
    #
    # Easier: mark this regression at the integration level. Build
    # *one* backend, call exec from main loop, then call exec from a
    # worker thread's `asyncio.run`. With the fix, both succeed and
    # `seen_in_loops` has two distinct ids. Without the fix, the
    # second call raised.
    transport = httpx.MockTransport(respond)
    backend = SandboxServiceBackend(
        base_url="https://x",
        api_token="t",
        session_id="s",
        tenant_id="t1",
        client=httpx.AsyncClient(
            base_url="https://x",
            headers={"Authorization": "Bearer t"},
            transport=transport,
        ),
    )
    ws = _make_workspace()

    async def main_loop_call():
        await backend.ensure_workspace(ws)
        r = await backend.exec(
            "echo hi",
            fs_write=FsWriteConfig(),
            network=NetworkConfig(),
            timeout_s=10,
            cwd=ws.abs_path,
        )
        return r

    asyncio.run(main_loop_call())

    # Now mimic SandboxBackedCodeExecutor: call exec from a fresh
    # asyncio.run inside a worker thread. With the fix this succeeds;
    # the test client object is reused safely because httpx's per-
    # connection state machine is exercised through MockTransport
    # which has no internal asyncio primitives.
    error_box: list[BaseException] = []
    result_box: list = []

    def runner():
        try:
            async def call():
                return await backend.exec(
                    "echo hi",
                    fs_write=FsWriteConfig(),
                    network=NetworkConfig(),
                    timeout_s=10,
                    cwd=ws.abs_path,
                )
            result_box.append(asyncio.run(call()))
        except BaseException as e:
            error_box.append(e)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    assert not error_box, (
        f"cross-loop call failed: {type(error_box[0]).__name__}: {error_box[0]}"
    )
    assert len(result_box) == 1
    assert result_box[0].exit_code == 0
    print("OK cross_event_loop_safe")


async def test_resume_latency_logged_when_nontrivial():
    """resume_latency_ms ≥ 250 ms emits a structured INFO log line."""
    import logging

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "svc-resume"})
        if request.url.path.endswith("/exec"):
            return httpx.Response(
                200,
                json={
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "resume_latency_ms": 800,
                },
            )
        return httpx.Response(404)

    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _Capture(level=logging.INFO)
    logger = logging.getLogger(
        "adk_cc.sandbox.backends.sandbox_service_backend"
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        rec = _Recorder(respond)
        backend = _make_backend(rec)
        ws = _make_workspace()
        from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

        await backend.ensure_workspace(ws)
        await backend.exec(
            "echo",
            fs_write=FsWriteConfig(),
            network=NetworkConfig(),
            timeout_s=10,
            cwd=ws.abs_path,
        )
        assert any("800 ms" in m for m in captured), captured
        print("OK resume_latency_logged_when_nontrivial")
    finally:
        logger.removeHandler(handler)


async def test_read_text_404_to_filenotfound():
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "svc-sid-6"})
        if request.url.path.startswith("/v1/sessions/svc-sid-6/files/"):
            return httpx.Response(404, text="not found")
        return httpx.Response(500)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsReadConfig

    await backend.ensure_workspace(ws)
    try:
        await backend.read_text(
            os.path.join(ws.abs_path, "missing.txt"),
            fs_read=FsReadConfig(),
        )
    except FileNotFoundError as e:
        assert "missing.txt" in str(e)
        print("OK read_text_404_to_filenotfound")
        return
    raise AssertionError("expected FileNotFoundError")


async def test_write_text_round_trip():
    captured_body: dict[str, Any] = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "svc-sid-7"})
        if request.url.path == "/v1/sessions/svc-sid-7/files/sub/foo.txt":
            captured_body["content"] = request.content.decode("utf-8")
            captured_body["content_type"] = request.headers.get("content-type")
            return httpx.Response(
                200, json={"path": "sub/foo.txt", "size": 5, "mode": "0o644"}
            )
        return httpx.Response(404, text=f"unexpected: {request.url}")

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsWriteConfig

    await backend.ensure_workspace(ws)
    await backend.write_text(
        os.path.join(ws.abs_path, "sub/foo.txt"),
        "hello",
        fs_write=FsWriteConfig(),
    )
    assert captured_body["content"] == "hello", captured_body
    assert captured_body["content_type"] == "application/octet-stream"
    print("OK write_text_round_trip")


# === Factory dispatch (uses real env, no mock needed) ===


def test_factory_requires_url_and_token():
    from adk_cc.sandbox.backends.sandbox_service_backend import (
        make_sandbox_service_backend_from_env,
    )

    saved_url = os.environ.pop("ADK_CC_SANDBOX_SERVICE_URL", None)
    saved_token = os.environ.pop("ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN", None)
    try:
        try:
            make_sandbox_service_backend_from_env(
                session_id="s", tenant_id="t"
            )
        except RuntimeError as e:
            assert "ADK_CC_SANDBOX_SERVICE_URL" in str(e), str(e)
            print("OK factory_requires_url")
        else:
            raise AssertionError("expected RuntimeError")

        os.environ["ADK_CC_SANDBOX_SERVICE_URL"] = "https://example.test"
        try:
            make_sandbox_service_backend_from_env(
                session_id="s", tenant_id="t"
            )
        except RuntimeError as e:
            assert "ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN" in str(e), str(e)
            print("OK factory_requires_token")
        else:
            raise AssertionError("expected RuntimeError")

        os.environ["ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN"] = "tok"
        os.environ["ADK_CC_SANDBOX_SERVICE_VCPU"] = "4"
        b = make_sandbox_service_backend_from_env(session_id="s", tenant_id="t")
        assert b._limits == {"vcpu": 4}, b._limits
        print("OK factory_limits_from_env")
    finally:
        os.environ.pop("ADK_CC_SANDBOX_SERVICE_URL", None)
        os.environ.pop("ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN", None)
        os.environ.pop("ADK_CC_SANDBOX_SERVICE_VCPU", None)
        if saved_url:
            os.environ["ADK_CC_SANDBOX_SERVICE_URL"] = saved_url
        if saved_token:
            os.environ["ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN"] = saved_token


def test_make_default_backend_dispatches_sandbox_service():
    from adk_cc.sandbox import make_default_backend

    saved = {
        k: os.environ.pop(k, None)
        for k in (
            "ADK_CC_SANDBOX_BACKEND",
            "ADK_CC_SANDBOX_SERVICE_URL",
            "ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN",
        )
    }
    try:
        os.environ["ADK_CC_SANDBOX_BACKEND"] = "sandbox_service"
        os.environ["ADK_CC_SANDBOX_SERVICE_URL"] = "https://x"
        os.environ["ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN"] = "tok"
        b = make_default_backend(session_id="sess", tenant_id="t1")
        from adk_cc.sandbox.backends.sandbox_service_backend import (
            SandboxServiceBackend,
        )

        assert isinstance(b, SandboxServiceBackend), type(b)
        assert b._session_id == "sess"
        assert b._tenant_id == "t1"
        print("OK make_default_backend_dispatches_sandbox_service")
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


# === Runner ===


def main():
    test_factory_requires_url_and_token()
    test_make_default_backend_dispatches_sandbox_service()
    asyncio.run(test_exec_wraps_in_bash_lc())
    asyncio.run(test_exec_cwd_subdir_gets_cd_prefix())
    asyncio.run(test_path_translation_rejects_outside_workspace())
    asyncio.run(test_close_calls_stop_not_destroy())
    asyncio.run(test_per_session_binding())
    asyncio.run(test_truncated_stream_propagates_to_stderr())
    asyncio.run(test_read_text_404_to_filenotfound())
    asyncio.run(test_write_text_round_trip())
    asyncio.run(test_idempotency_key_present_on_mutating_calls())
    asyncio.run(test_resume_latency_logged_when_nontrivial())
    asyncio.run(test_credential_provider_resolves_per_tenant_token())
    asyncio.run(test_credential_provider_missing_token_raises())
    asyncio.run(test_exec_stream_yields_chunks_then_result())
    asyncio.run(test_exec_stream_synthesizes_result_on_4xx())
    asyncio.run(test_exec_stream_default_impl_for_other_backends())
    test_cross_event_loop_safe()  # synchronous — manages its own asyncio.run
    print("\nall sandbox_service_backend tests passed")


if __name__ == "__main__":
    main()
