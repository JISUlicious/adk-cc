"""Unit tests for `DaytonaBackend`.

All HTTP traffic is mocked at the `httpx` boundary via
`httpx.MockTransport`, so these tests don't need a live Daytona
deployment. There's also one optional integration test at the end
that runs the 7-step smoke flow against a real Daytona — skipped
unless `ADK_CC_DAYTONA_API_URL` AND `ADK_CC_DAYTONA_API_KEY` are
present in the environment.

Coverage:
  - factory dispatch (env-var driven)
  - ensure_workspace posts /api/sandbox with NO resource fields,
    polls /api/sandbox/{id} until state=started, caches the id
  - exec posts to {proxy}/toolbox/{id}/process/execute, parses
    `{exitCode, result}` into `ExecResult(stdout=result, stderr="")`
  - read_text GETs /toolbox/{id}/files/download with `path=` query
  - write_text POSTs /toolbox/{id}/files/upload multipart, `path=`
    in query, `file` as the form field
  - Idempotency-Key on every mutating control-plane call; absent
    on toolbox-proxy calls
  - Error normalizer: 401→SandboxViolation, 403→SandboxViolation,
    404 on file read→FileNotFoundError, 429→SandboxViolation
  - Allow-path enforcement raises SandboxViolation BEFORE HTTP
  - exec transport / 4xx errors return ExecResult(-1, stderr=...)
    rather than raising
  - close() POSTs /stop by default; DELETEs when delete_on_close=1;
    swallows exceptions

Run: `uv run python tests/test_daytona_backend.py`
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")


# === Helpers ===


class _Recorder:
    """Captures every request the backend makes, returns canned responses.

    One MockTransport per test; the backend's two `httpx.AsyncClient`
    instances (api + proxy) get wired through the SAME transport, so
    the handler sees BOTH control-plane and toolbox-proxy traffic and
    branches on `request.url.host` to distinguish them.
    """

    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return await self._responder(request)

        return httpx.MockTransport(handler)


def _make_backend(recorder: _Recorder, **overrides):
    from adk_cc.sandbox.backends.daytona_backend import DaytonaBackend

    # Test-injection: the backend treats `client` as a SHARED client
    # used for both api and proxy calls (we pass the same MockTransport
    # to both; the handler branches by request.url.host).
    client = httpx.AsyncClient(
        headers={"Authorization": "Bearer test-token"},
        transport=recorder.transport(),
    )
    kwargs = dict(
        session_id="adkcc-sess-1",
        tenant_id="acme",
        api_url="http://api.test",
        proxy_url="http://proxy.test",
        api_key="test-token",
        snapshot="adk-cc-base:latest",
        workspace_path="/sandbox/wks",
        autostop_minutes=15,
        autodelete_minutes=1440,
        client=client,
    )
    kwargs.update(overrides)
    return DaytonaBackend(**kwargs)


def _make_workspace():
    from adk_cc.sandbox.workspace import WorkspaceRoot

    return WorkspaceRoot(
        tenant_id="acme",
        session_id="adkcc-sess-1",
        abs_path="/sandbox/wks",
    )


def _is_api(request: httpx.Request) -> bool:
    return request.url.host == "api.test"


def _is_proxy(request: httpx.Request) -> bool:
    return request.url.host == "proxy.test"


# === Tests ===


async def test_ensure_workspace_creates_and_polls():
    """POST /api/sandbox with no resource fields; poll /api/sandbox/{id}
    until state=started; cache the id; idempotent on second call."""
    poll_count = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        path = request.url.path
        if request.method == "POST" and path == "/api/sandbox":
            body = json.loads(request.content.decode())
            # No resource fields — snapshot is in play, so cpu/memory/disk
            # must be absent (Daytona 400s otherwise).
            for forbidden in ("cpu", "memory", "disk"):
                assert forbidden not in body, body
            assert body["snapshot"] == "adk-cc-base:latest"
            assert body["autoStopInterval"] == 15
            assert body["autoDeleteInterval"] == 1440
            assert "name" in body
            return httpx.Response(
                200,
                json={
                    "id": "sbx-001",
                    "state": "creating",
                    "snapshot": "adk-cc-base:latest",
                    "user": "daytona",
                },
            )
        if request.method == "GET" and path == "/api/sandbox/sbx-001":
            poll_count += 1
            # Started on the second poll to exercise the loop.
            state = "creating" if poll_count == 1 else "started"
            return httpx.Response(
                200,
                json={"id": "sbx-001", "state": state},
            )
        return httpx.Response(404, json={"message": f"unexpected {path}"})

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()

    await backend.ensure_workspace(ws)
    assert backend._sandbox_id == "sbx-001"

    # Idempotent: a second call doesn't issue any new requests.
    n_before = len(rec.requests)
    await backend.ensure_workspace(ws)
    assert len(rec.requests) == n_before
    print("OK ensure_workspace_creates_and_polls")


async def test_idempotency_key_on_control_plane_only():
    """Every mutating control-plane request carries Idempotency-Key;
    toolbox-proxy calls do not."""

    async def respond(request: httpx.Request) -> httpx.Response:
        if _is_api(request) and request.method == "POST" and request.url.path == "/api/sandbox":
            assert "idempotency-key" in {k.lower() for k in request.headers}
            return httpx.Response(
                200, json={"id": "sbx-001", "state": "started"}
            )
        if _is_api(request) and request.url.path == "/api/sandbox/sbx-001":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if _is_proxy(request) and request.method == "POST":
            assert "idempotency-key" not in {k.lower() for k in request.headers}, (
                f"toolbox-proxy POST should NOT carry Idempotency-Key, got: "
                f"{dict(request.headers)}"
            )
            return httpx.Response(200, json={"exitCode": 0, "result": "ok\n"})
        return httpx.Response(404, json={"message": "?"})

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

    await backend.ensure_workspace(ws)
    await backend.exec(
        "echo ok",
        fs_write=FsWriteConfig(allow_paths=(ws.abs_path,)),
        network=NetworkConfig(),
        timeout_s=10,
        cwd=ws.abs_path,
    )
    print("OK idempotency_key_on_control_plane_only")


async def test_exec_parses_exitcode_and_result():
    """`exec()` returns ExecResult with exit_code from `exitCode` field
    and stdout=result (Daytona merges stdout+stderr into `result`)."""

    async def respond(request: httpx.Request) -> httpx.Response:
        if _is_api(request) and request.url.path == "/api/sandbox":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if _is_api(request) and request.url.path == "/api/sandbox/sbx-001":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if _is_proxy(request) and request.url.path == "/toolbox/sbx-001/process/execute":
            body = json.loads(request.content.decode())
            assert body["command"] == "echo hi"
            assert body["cwd"] == "/sandbox/wks"
            assert body["timeout"] == 10
            return httpx.Response(
                200,
                json={"exitCode": 0, "result": "hi\n"},
            )
        return httpx.Response(404, json={"message": "?"})

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

    await backend.ensure_workspace(ws)
    res = await backend.exec(
        "echo hi",
        fs_write=FsWriteConfig(allow_paths=(ws.abs_path,)),
        network=NetworkConfig(),
        timeout_s=10,
        cwd=ws.abs_path,
    )
    assert res.exit_code == 0, res
    assert res.stdout == "hi\n"
    assert res.stderr == ""
    print("OK exec_parses_exitcode_and_result")


async def test_exec_transport_error_returns_failed_execresult():
    """Transport errors during exec return ExecResult(exit_code=-1)
    rather than raising — matches SandboxServiceBackend convention."""

    async def respond(request: httpx.Request) -> httpx.Response:
        if _is_api(request) and request.url.path == "/api/sandbox":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if _is_api(request) and request.url.path == "/api/sandbox/sbx-001":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if _is_proxy(request):
            raise httpx.ConnectError("simulated network drop")
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
        timeout_s=10,
        cwd=ws.abs_path,
    )
    assert res.exit_code == -1
    assert "transport error" in res.stderr
    print("OK exec_transport_error_returns_failed_execresult")


async def test_exec_4xx_returns_failed_execresult():
    """4xx from the toolbox proxy on exec returns ExecResult(-1)
    rather than raising."""

    async def respond(request: httpx.Request) -> httpx.Response:
        if _is_api(request) and request.url.path == "/api/sandbox":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if _is_api(request) and request.url.path == "/api/sandbox/sbx-001":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if _is_proxy(request):
            return httpx.Response(400, json={"message": "bad command"})
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
        timeout_s=10,
        cwd=ws.abs_path,
    )
    assert res.exit_code == -1
    assert "400" in res.stderr
    print("OK exec_4xx_returns_failed_execresult")


async def test_read_text_query_param_and_decode():
    """GET /toolbox/{id}/files/download?path=... returns the body
    as decoded utf-8."""

    async def respond(request: httpx.Request) -> httpx.Response:
        if _is_api(request):
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if _is_proxy(request) and request.url.path == "/toolbox/sbx-001/files/download":
            assert request.url.params["path"] == "/sandbox/wks/foo.txt"
            return httpx.Response(
                200,
                content=b"hello world",
                headers={"Content-Type": "application/octet-stream"},
            )
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsReadConfig

    await backend.ensure_workspace(ws)
    text = await backend.read_text(
        "/sandbox/wks/foo.txt",
        fs_read=FsReadConfig(allow_paths=("/sandbox/wks/**",)),
    )
    assert text == "hello world"
    print("OK read_text_query_param_and_decode")


async def test_read_text_404_raises_file_not_found():
    async def respond(request: httpx.Request) -> httpx.Response:
        if _is_api(request):
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if _is_proxy(request):
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsReadConfig

    await backend.ensure_workspace(ws)
    try:
        await backend.read_text(
            "/sandbox/wks/missing.txt",
            fs_read=FsReadConfig(allow_paths=("/sandbox/wks/**",)),
        )
    except FileNotFoundError as e:
        assert "/sandbox/wks/missing.txt" in str(e)
        print("OK read_text_404_raises_file_not_found")
        return
    raise AssertionError("expected FileNotFoundError")


async def test_write_text_multipart_path_query_file_form():
    """Upload sends `path=` as a QUERY parameter (not form field) and
    `file` as the multipart form field. Server uses the query path
    for placement; the form filename is cosmetic."""

    captured_body: dict = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        if _is_api(request):
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if _is_proxy(request) and request.url.path == "/toolbox/sbx-001/files/upload":
            # `path` is in the query string, not the multipart form.
            assert request.url.params["path"] == "/sandbox/wks/out.txt"
            content_type = request.headers.get("content-type", "")
            assert content_type.startswith("multipart/form-data"), content_type
            captured_body["body"] = request.content
            return httpx.Response(200)
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    from adk_cc.sandbox.config import FsWriteConfig

    await backend.ensure_workspace(ws)
    await backend.write_text(
        "/sandbox/wks/out.txt",
        "the content",
        fs_write=FsWriteConfig(allow_paths=("/sandbox/wks/**",)),
    )
    # The multipart body must contain the form-data part named "file"
    # with our payload bytes.
    body = captured_body["body"]
    assert b'name="file"' in body, body[:200]
    assert b"the content" in body, body[:200]
    print("OK write_text_multipart_path_query_file_form")


async def test_create_body_elides_resource_fields():
    """`POST /api/sandbox` body must NOT carry cpu/memory/disk when a
    snapshot is set — Daytona rejects with 400."""

    async def respond(request: httpx.Request) -> httpx.Response:
        if _is_api(request) and request.method == "POST" and request.url.path == "/api/sandbox":
            body = json.loads(request.content.decode())
            for forbidden in ("cpu", "memory", "disk"):
                assert forbidden not in body, body
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if _is_api(request):
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    await backend.ensure_workspace(ws)
    print("OK create_body_elides_resource_fields")


async def test_auth_failure_raises_sandbox_violation():
    """401 from any endpoint → SandboxViolation('auth failed')."""
    from adk_cc.sandbox.config import SandboxViolation

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid credentials"})

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    try:
        await backend.ensure_workspace(ws)
    except SandboxViolation as e:
        assert "auth failed" in str(e).lower()
        print("OK auth_failure_raises_sandbox_violation")
        return
    raise AssertionError("expected SandboxViolation on 401")


async def test_429_raises_sandbox_violation():
    """429 → SandboxViolation('quota exhausted')."""
    from adk_cc.sandbox.config import SandboxViolation

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "Too Many Requests"})

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    try:
        await backend.ensure_workspace(ws)
    except SandboxViolation as e:
        assert "quota" in str(e).lower()
        print("OK 429_raises_sandbox_violation")
        return
    raise AssertionError("expected SandboxViolation on 429")


async def test_terminal_failure_state_raises():
    """Sandbox transitioning to a terminal failure state aborts the poll
    loop with SandboxViolation."""
    from adk_cc.sandbox.config import SandboxViolation

    poll = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal poll
        if request.method == "POST" and request.url.path == "/api/sandbox":
            return httpx.Response(200, json={"id": "sbx-bad", "state": "creating"})
        poll += 1
        return httpx.Response(
            200,
            json={
                "id": "sbx-bad",
                "state": "error",
                "errorReason": "build failed: missing layer",
            },
        )

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    try:
        await backend.ensure_workspace(ws)
    except SandboxViolation as e:
        assert "terminal" in str(e).lower() or "build failed" in str(e)
        print("OK terminal_failure_state_raises")
        return
    raise AssertionError("expected SandboxViolation on terminal state")


async def test_allow_path_enforced_before_http():
    """fs_read / fs_write allow_paths violations raise SandboxViolation
    BEFORE any HTTP request goes out."""
    from adk_cc.sandbox.config import FsReadConfig, FsWriteConfig, SandboxViolation

    async def respond(request: httpx.Request) -> httpx.Response:
        if _is_api(request):
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        raise AssertionError(f"unexpected HTTP call: {request.url}")

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    await backend.ensure_workspace(ws)
    n_before = len(rec.requests)

    # Read outside workspace.
    try:
        await backend.read_text(
            "/etc/passwd",
            fs_read=FsReadConfig(allow_paths=("/sandbox/wks/**",)),
        )
    except SandboxViolation:
        pass
    else:
        raise AssertionError("expected SandboxViolation on /etc/passwd read")

    # Write outside workspace.
    try:
        await backend.write_text(
            "/tmp/escape.txt",
            "x",
            fs_write=FsWriteConfig(allow_paths=("/sandbox/wks/**",)),
        )
    except SandboxViolation:
        pass
    else:
        raise AssertionError("expected SandboxViolation on /tmp/escape.txt write")

    # No new HTTP calls — both failures happened client-side.
    assert len(rec.requests) == n_before, (
        f"client-side allow-path check should not trigger HTTP; "
        f"got {len(rec.requests) - n_before} new requests"
    )
    print("OK allow_path_enforced_before_http")


async def test_close_posts_stop_by_default():
    """`close()` calls POST /api/sandbox/{id}/stop with an
    Idempotency-Key (preserves the sandbox for resume)."""
    saw_stop = False

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal saw_stop
        if request.method == "POST" and request.url.path == "/api/sandbox":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if request.url.path == "/api/sandbox/sbx-001":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if request.method == "POST" and request.url.path == "/api/sandbox/sbx-001/stop":
            assert "idempotency-key" in {k.lower() for k in request.headers}
            saw_stop = True
            return httpx.Response(200, json={"id": "sbx-001", "state": "stopped"})
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    await backend.ensure_workspace(ws)
    await backend.close()
    assert saw_stop, "expected POST /api/sandbox/sbx-001/stop"
    print("OK close_posts_stop_by_default")


async def test_close_deletes_when_delete_on_close_true():
    """delete_on_close=True → DELETE /api/sandbox/{id}, not stop."""
    saw_delete = False

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal saw_delete
        if request.method == "POST" and request.url.path == "/api/sandbox":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if request.url.path == "/api/sandbox/sbx-001" and request.method == "GET":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if request.method == "DELETE" and request.url.path == "/api/sandbox/sbx-001":
            saw_delete = True
            return httpx.Response(200, json={"id": "sbx-001"})
        return httpx.Response(404)

    rec = _Recorder(respond)
    backend = _make_backend(rec, delete_on_close=True)
    ws = _make_workspace()
    await backend.ensure_workspace(ws)
    await backend.close()
    assert saw_delete, "expected DELETE on close when delete_on_close=True"
    print("OK close_deletes_when_delete_on_close_true")


async def test_close_swallows_exceptions():
    """`close()` is best-effort and never raises."""

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sandbox":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if request.url.path == "/api/sandbox/sbx-001" and request.method == "GET":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        # Stop returns 500 — close() should swallow.
        return httpx.Response(500, text="boom")

    rec = _Recorder(respond)
    backend = _make_backend(rec)
    ws = _make_workspace()
    await backend.ensure_workspace(ws)
    # Must not raise:
    await backend.close()
    print("OK close_swallows_exceptions")


async def test_factory_from_env_dispatches():
    """`make_default_backend()` with ADK_CC_SANDBOX_BACKEND=daytona
    constructs a DaytonaBackend with the env values."""
    from adk_cc.sandbox.backends.daytona_backend import DaytonaBackend

    old = {
        k: os.environ.get(k)
        for k in (
            "ADK_CC_SANDBOX_BACKEND",
            "ADK_CC_DAYTONA_API_URL",
            "ADK_CC_DAYTONA_API_KEY",
            "ADK_CC_DAYTONA_SNAPSHOT",
            "ADK_CC_DAYTONA_PROXY_URL",
        )
    }
    try:
        os.environ["ADK_CC_SANDBOX_BACKEND"] = "daytona"
        os.environ["ADK_CC_DAYTONA_API_URL"] = "http://daytona.local:3000"
        os.environ["ADK_CC_DAYTONA_API_KEY"] = "dtn_test_xyz"
        os.environ["ADK_CC_DAYTONA_SNAPSHOT"] = "my-snap"
        os.environ.pop("ADK_CC_DAYTONA_PROXY_URL", None)

        from adk_cc.sandbox import make_default_backend

        b = make_default_backend(session_id="s1", tenant_id="t1")
        assert isinstance(b, DaytonaBackend)
        assert b._api_base == "http://daytona.local:3000"
        # Proxy URL derived by port swap when env unset.
        assert b._proxy_base == "http://daytona.local:4000"
        assert b._snapshot == "my-snap"
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("OK factory_from_env_dispatches")


# === Optional integration test ===


async def test_integration_smoke():
    """The 7-step live smoke flow against a real Daytona instance.

    Skipped unless `ADK_CC_DAYTONA_API_URL` + `ADK_CC_DAYTONA_API_KEY`
    are set. When run, it codifies the contract we verified during
    backend design (sandbox create → poll → exec → upload → download
    → close).
    """
    if not (
        os.environ.get("ADK_CC_DAYTONA_API_URL")
        and os.environ.get("ADK_CC_DAYTONA_API_KEY")
    ):
        print("SKIP integration_smoke (ADK_CC_DAYTONA_API_URL / _API_KEY unset)")
        return

    from adk_cc.sandbox.backends.daytona_backend import (
        make_daytona_backend_from_env,
    )
    from adk_cc.sandbox.config import FsReadConfig, FsWriteConfig, NetworkConfig
    from adk_cc.sandbox.workspace import WorkspaceRoot

    backend = make_daytona_backend_from_env(
        session_id=f"smoke-{os.getpid()}",
        tenant_id="smoke-tenant",
    )
    # Live test uses /home/daytona — the real cwd inside the
    # daytonaio/sandbox:0.5.0-slim default snapshot. The unit tests
    # above use /sandbox/wks because macOS's realpath() rewrites
    # /home/* through /System/Volumes/Data/home/* in
    # WorkspaceRoot.__post_init__; the integration runs against a
    # Linux sandbox where /home/daytona is real.
    ws = WorkspaceRoot(
        tenant_id="smoke-tenant",
        session_id=f"smoke-{os.getpid()}",
        abs_path="/home/daytona",
    )
    try:
        await backend.ensure_workspace(ws)
        # Step 4: exec
        res = await backend.exec(
            "echo hello && pwd",
            fs_write=FsWriteConfig(allow_paths=("/home/daytona/**",)),
            network=NetworkConfig(),
            timeout_s=15,
            cwd="/home/daytona",
        )
        assert res.exit_code == 0, res
        assert "hello" in res.stdout
        # Steps 5-6: write then read
        await backend.write_text(
            "/home/daytona/smoke.txt",
            "round-trip ok",
            fs_write=FsWriteConfig(allow_paths=("/home/daytona/**",)),
        )
        got = await backend.read_text(
            "/home/daytona/smoke.txt",
            fs_read=FsReadConfig(allow_paths=("/home/daytona/**",)),
        )
        assert got == "round-trip ok", got
        print("OK integration_smoke")
    finally:
        # Step 7: close — always runs, never raises.
        await backend.close()


# === Driver ===


async def test_create_injects_sandbox_env():
    """ensure_workspace bakes resolved env (static + host passthrough +
    per-tenant credential) into the POST /api/sandbox `env` field."""
    from adk_cc.sandbox.sandbox_env import SandboxEnvSpec
    from adk_cc.credentials.impls import InMemoryCredentialProvider

    os.environ["ADK_CC_DAYTONA_TEST_PT"] = "from-host"
    try:
        prov = InMemoryCredentialProvider(shared=False)
        await prov.put(tenant_id="acme", key="gh_pat", value="ghp_secret")
        spec = SandboxEnvSpec(
            static={"TZ": "UTC"},
            passthrough=("ADK_CC_DAYTONA_TEST_PT",),
            credentials={"GITHUB_TOKEN": "gh_pat"},
        )
        captured: dict = {}

        async def respond(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST" and path == "/api/sandbox":
                captured["body"] = json.loads(request.content.decode())
                return httpx.Response(
                    200, json={"id": "sbx-001", "state": "started"}
                )
            if request.method == "GET" and path == "/api/sandbox/sbx-001":
                return httpx.Response(
                    200, json={"id": "sbx-001", "state": "started"}
                )
            return httpx.Response(404, json={"message": f"unexpected {path}"})

        rec = _Recorder(respond)
        backend = _make_backend(rec, credentials=prov, env_spec=spec)
        await backend.ensure_workspace(_make_workspace())
        env = captured["body"].get("env")
        assert env == {
            "TZ": "UTC",
            "ADK_CC_DAYTONA_TEST_PT": "from-host",
            "GITHUB_TOKEN": "ghp_secret",
        }, env
        print("OK create_injects_sandbox_env")
    finally:
        os.environ.pop("ADK_CC_DAYTONA_TEST_PT", None)


async def test_create_omits_env_when_no_spec():
    """No env_spec (or an empty one) → the create payload carries NO `env`
    field, so the v1 behavior is byte-for-byte unchanged."""
    captured: dict = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/sandbox":
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        if request.method == "GET" and path == "/api/sandbox/sbx-001":
            return httpx.Response(200, json={"id": "sbx-001", "state": "started"})
        return httpx.Response(404, json={"message": f"unexpected {path}"})

    rec = _Recorder(respond)
    backend = _make_backend(rec)  # no env_spec
    await backend.ensure_workspace(_make_workspace())
    assert "env" not in captured["body"], captured["body"]
    print("OK create_omits_env_when_no_spec")


def main():
    tests = [
        test_ensure_workspace_creates_and_polls,
        test_idempotency_key_on_control_plane_only,
        test_exec_parses_exitcode_and_result,
        test_exec_transport_error_returns_failed_execresult,
        test_exec_4xx_returns_failed_execresult,
        test_read_text_query_param_and_decode,
        test_read_text_404_raises_file_not_found,
        test_write_text_multipart_path_query_file_form,
        test_create_body_elides_resource_fields,
        test_auth_failure_raises_sandbox_violation,
        test_429_raises_sandbox_violation,
        test_terminal_failure_state_raises,
        test_allow_path_enforced_before_http,
        test_close_posts_stop_by_default,
        test_close_deletes_when_delete_on_close_true,
        test_close_swallows_exceptions,
        test_factory_from_env_dispatches,
        test_create_injects_sandbox_env,
        test_create_omits_env_when_no_spec,
        test_integration_smoke,
    ]
    for t in tests:
        asyncio.run(t())
    print("\nall daytona-backend tests passed")


if __name__ == "__main__":
    main()
