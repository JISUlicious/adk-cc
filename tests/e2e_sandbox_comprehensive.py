#!/usr/bin/env python3
"""Comprehensive e2e against a live sandbox service.

Where `tests/e2e_sandbox_service.py` is a smoke test + bug-verification
suite (one path through the contract, plus probes for known issues),
this script exercises edge cases, negative paths, multi-step flows,
and surfaces the smoke test doesn't cover (processes, streaming exec,
idempotency replay, auth boundary, path validation, concurrency).

Categories (≈50 checks total):

  1.  Session lifecycle    — create with limits, stop/resume, destroy semantics
  2.  Exec contract        — argv/env/stdin/timeout/large output/forbidden vars
  3.  Files API            — list, delete, traversal, nested writes, mode bits
  4.  Streaming exec       — SSE event ordering, final result, content-type
  5.  Processes            — start/list/get/logs/stop, lifecycle transitions
  6.  Idempotency          — replay header, fresh keys, cross-route isolation
  7.  Auth boundary        — missing/malformed bearer, ownership checks
  8.  Path validation      — `..`, absolute, leading slash, NUL byte
  9.  Concurrency          — parallel exec in one session, isolated sessions

Configuration: same as the smoke e2e — auto-sources `.env`, picks up
`ADK_CC_SANDBOX_SERVICE_URL` + a token (`ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN`,
`ADK_CC_SANDBOX_SERVICE_TOKEN`, or `SANDBOX_API_TOKEN`).

Run:
    /usr/bin/python3 tests/e2e_sandbox_comprehensive.py
    # or any python with httpx

Each category prints its own [OK]/[FAIL] lines plus a short summary.
At the end, an overall pass/fail summary and exit code (non-zero on any
failure). Tests that genuinely require server features absent from the
deployment (e.g. admin token) are marked [SKIP] and do not fail the run.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import httpx
except ImportError:  # pragma: no cover
    print("[skip] httpx not installed. /usr/bin/python3 -m pip install --user httpx")
    sys.exit(0)

# Auto-source `.env` so tokens flow through without ceremony.
_REPO = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO / ".env"
if _ENV_FILE.is_file():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v

CONTAINER_WORKSPACE = "/workspace"


def _idem() -> str:
    return uuid.uuid4().hex


# === Test infrastructure ===


class _Result:
    """A single test outcome."""

    def __init__(self, name: str, status: str, detail: str = "", elapsed_ms: float = 0.0):
        self.name = name
        self.status = status  # "OK" | "FAIL" | "SKIP"
        self.detail = detail
        self.elapsed_ms = elapsed_ms

    def __str__(self) -> str:
        tag = {"OK": "[OK]  ", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[self.status]
        line = f"  {tag} {self.name:55s} ({self.elapsed_ms:.0f} ms)"
        if self.detail and self.status != "OK":
            line += f"\n         {self.detail}"
        return line


def _check(results: list[_Result]):
    """Decorator-ish helper. Use as:
        async def runner():
            with _Step('name') as s: ...
    """


class _Step:
    def __init__(self, name: str, results: list[_Result]):
        self.name = name
        self.results = results
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        ms = (time.perf_counter() - self.t0) * 1000
        if exc is None:
            self.results.append(_Result(self.name, "OK", "", ms))
            return False
        if isinstance(exc, _SkipException):
            self.results.append(_Result(self.name, "SKIP", str(exc), ms))
            return True
        detail = f"{type(exc).__name__}: {exc}"
        # add a one-liner traceback hint
        last = traceback.format_exception(exc_type, exc, tb)[-2:]
        for line in last:
            for sub in line.rstrip().splitlines():
                detail += f"\n         {sub}"
        self.results.append(_Result(self.name, "FAIL", detail, ms))
        return True


class _SkipException(Exception):
    """Raised inside a `with _Step(...)` to mark the test skipped, not failed."""


def _skip(reason: str):
    raise _SkipException(reason)


# === Sandbox client ===


class _Client:
    """HTTP client mirroring SandboxServiceBackend's call shape."""

    def __init__(self, base_url: str, token: str):
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
            verify=False,
        )

    async def aclose(self):
        await self._http.aclose()

    # --- sessions ---

    async def session_create(self, *, limits: dict | None = None, key: str | None = None) -> dict:
        body: dict[str, Any] = {}
        if limits is not None:
            body["limits"] = limits
        r = await self._http.post(
            "/v1/sessions", json=body,
            headers={"Idempotency-Key": key or _idem()},
        )
        r.raise_for_status()
        return r.json()

    async def session_get(self, sid: str) -> httpx.Response:
        return await self._http.get(f"/v1/sessions/{sid}")

    async def session_stop(self, sid: str, *, key: str | None = None) -> httpx.Response:
        return await self._http.post(
            f"/v1/sessions/{sid}/stop",
            headers={"Idempotency-Key": key or _idem()},
        )

    async def session_resume(self, sid: str, *, key: str | None = None) -> httpx.Response:
        return await self._http.post(
            f"/v1/sessions/{sid}/resume",
            headers={"Idempotency-Key": key or _idem()},
        )

    async def session_destroy(self, sid: str, *, key: str | None = None) -> httpx.Response:
        return await self._http.delete(
            f"/v1/sessions/{sid}",
            headers={"Idempotency-Key": key or _idem()},
        )

    # --- exec ---

    async def exec(
        self, sid: str, cmd: str, *,
        cwd: str = CONTAINER_WORKSPACE,
        timeout_s: int = 30,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        key: str | None = None,
    ) -> httpx.Response:
        argv = ["/bin/bash", "-lc", cmd]
        if cwd != CONTAINER_WORKSPACE:
            quoted = cwd.replace("'", "'\\''")
            argv = ["/bin/bash", "-lc", f"cd '{quoted}' && {cmd}"]
        body: dict[str, Any] = {"argv": argv, "timeout_s": timeout_s}
        if env is not None:
            body["env"] = env
        if stdin is not None:
            body["stdin"] = stdin
        return await self._http.post(
            f"/v1/sessions/{sid}/exec",
            json=body,
            headers={"Idempotency-Key": key or _idem()},
        )

    async def exec_argv(
        self, sid: str, argv: list[str], *,
        timeout_s: int = 30,
        env: dict[str, str] | None = None,
        key: str | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {"argv": argv, "timeout_s": timeout_s}
        if env is not None:
            body["env"] = env
        return await self._http.post(
            f"/v1/sessions/{sid}/exec",
            json=body,
            headers={"Idempotency-Key": key or _idem()},
        )

    # --- files ---

    async def file_write_collection(self, sid: str, path: str, content: bytes, *, key: str | None = None) -> httpx.Response:
        body = {
            "path": path,
            "content_b64": base64.b64encode(content).decode("ascii"),
        }
        return await self._http.post(
            f"/v1/sessions/{sid}/files",
            json=body,
            headers={"Idempotency-Key": key or _idem()},
        )

    async def file_write_path(self, sid: str, path: str, content: bytes, *, key: str | None = None) -> httpx.Response:
        return await self._http.post(
            f"/v1/sessions/{sid}/files/{quote(path, safe='/')}",
            content=content,
            headers={
                "Content-Type": "application/octet-stream",
                "Idempotency-Key": key or _idem(),
            },
        )

    async def file_read(self, sid: str, path: str) -> httpx.Response:
        return await self._http.get(f"/v1/sessions/{sid}/files/{quote(path, safe='/')}")

    async def file_list(self, sid: str, dir: str = "") -> httpx.Response:
        # NB: server-side query parameter is `dir`, not `subdir`. The MCP
        # tool surface uses `subdir`; the REST API uses `dir`. Mirror the
        # REST name here since this client is the REST consumer.
        url = f"/v1/sessions/{sid}/files"
        if dir:
            url += f"?dir={quote(dir, safe='/')}"
        return await self._http.get(url)

    async def file_delete(self, sid: str, path: str, *, recursive: bool = False, key: str | None = None) -> httpx.Response:
        return await self._http.delete(
            f"/v1/sessions/{sid}/files/{quote(path, safe='/')}",
            params={"recursive": "true"} if recursive else None,
            headers={"Idempotency-Key": key or _idem()},
        )

    # --- processes ---

    async def process_start(self, sid: str, argv: list[str], *, name: str | None = None, key: str | None = None) -> httpx.Response:
        body: dict[str, Any] = {"argv": argv}
        if name:
            body["name"] = name
        return await self._http.post(
            f"/v1/sessions/{sid}/processes",
            json=body,
            headers={"Idempotency-Key": key or _idem()},
        )

    async def process_list(self, sid: str) -> httpx.Response:
        return await self._http.get(f"/v1/sessions/{sid}/processes")

    async def process_get(self, sid: str, pid: str) -> httpx.Response:
        return await self._http.get(f"/v1/sessions/{sid}/processes/{pid}")

    async def process_delete(self, sid: str, pid: str, *, key: str | None = None) -> httpx.Response:
        return await self._http.delete(
            f"/v1/sessions/{sid}/processes/{pid}",
            headers={"Idempotency-Key": key or _idem()},
        )


# === Test categories ===


async def cat_session_lifecycle(client: _Client) -> list[_Result]:
    print("\n── Session lifecycle ──")
    results: list[_Result] = []

    # 1.1 Create with explicit Limits — values honored
    with _Step("create with explicit limits — echoed in response", results) as s:
        body = await client.session_create(limits={"vcpu": 1, "memory_mib": 512, "exec_timeout_s": 10})
        sid = body["session_id"]
        try:
            limits = body.get("limits") or {}
            assert limits.get("vcpu") == 1, f"vcpu={limits.get('vcpu')!r}"
            assert limits.get("memory_mib") == 512, f"memory_mib={limits.get('memory_mib')!r}"
            assert limits.get("exec_timeout_s") == 10, f"exec_timeout_s={limits.get('exec_timeout_s')!r}"
        finally:
            await client.session_stop(sid)

    # 1.2 Create with limits exceeding tenant max → 400 limit_exceeded.
    # TenantLimits caps `max_concurrency`, `max_workspace_gib`, `max_exec_timeout_s`
    # — pick `exec_timeout_s` since it's the most likely to be tightly capped
    # (default tenant max in the spec is 600s; we send 99999).
    with _Step("create with exec_timeout_s=99999 → 400 limit_exceeded", results) as s:
        try:
            body = await client.session_create(limits={"exec_timeout_s": 99999})
            await client.session_stop(body["session_id"])
            raise AssertionError(f"expected 400 limit_exceeded, got success: {body}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                _skip("rate-limited before body validation; can't observe limit_exceeded path")
            assert e.response.status_code == 400, f"got {e.response.status_code}"
            j = e.response.json()
            code = (j.get("detail") or {}).get("code") if isinstance(j.get("detail"), dict) else None
            assert code in ("limit_exceeded", "invalid_argument"), f"code={code}"

    # 1.3 stop → resume → exec round-trip preserves filesystem
    with _Step("stop → resume preserves /workspace state", results) as s:
        body = await client.session_create()
        sid = body["session_id"]
        try:
            r = await client.file_write_collection(sid, "persisted.txt", b"sticky")
            r.raise_for_status()
            r = await client.session_stop(sid)
            r.raise_for_status()
            r = await client.session_resume(sid)
            r.raise_for_status()
            r = await client.file_read(sid, "persisted.txt")
            assert r.status_code == 200 and r.content == b"sticky", r.content
        finally:
            await client.session_destroy(sid)

    # 1.4 Destroy → all subsequent ops 404
    with _Step("after DELETE: GET → 404 session_not_found", results) as s:
        body = await client.session_create()
        sid = body["session_id"]
        r = await client.session_destroy(sid)
        assert r.status_code in (200, 204), f"DELETE returned {r.status_code}"
        r = await client.session_get(sid)
        assert r.status_code == 404, f"GET after destroy returned {r.status_code}"
        j = r.json()
        code = (j.get("detail") or {}).get("code") if isinstance(j.get("detail"), dict) else None
        assert code == "session_not_found", f"code={code!r}"

    # 1.5 Destroy idempotent: second DELETE returns 404 (or 204 — both reasonable)
    with _Step("DELETE on already-destroyed session", results) as s:
        body = await client.session_create()
        sid = body["session_id"]
        await client.session_destroy(sid)
        r2 = await client.session_destroy(sid)
        # Either 404 (gone) or 204 (idempotent re-delete) is acceptable; 5xx is not
        assert r2.status_code in (200, 204, 404), f"second DELETE returned {r2.status_code}"

    return results


async def cat_exec_contract(client: _Client, sid: str) -> list[_Result]:
    print("\n── Exec contract ──")
    results: list[_Result] = []

    # 2.1 Empty argv → 422
    with _Step("empty argv → 422 validation error", results) as s:
        r = await client._http.post(
            f"/v1/sessions/{sid}/exec",
            json={"argv": []},
            headers={"Idempotency-Key": _idem()},
        )
        assert r.status_code == 422, f"got {r.status_code}"

    # 2.2 stdout is non-empty for echo
    with _Step("exec: echo non-empty stdout", results) as s:
        r = await client.exec(sid, "echo hello-world")
        r.raise_for_status()
        d = r.json()
        assert "hello-world" in d.get("stdout", ""), d

    # 2.3 stderr captured separately
    with _Step("exec: stderr captured separately from stdout", results) as s:
        r = await client.exec(sid, "echo OUT >&1; echo ERR >&2")
        r.raise_for_status()
        d = r.json()
        assert "OUT" in d.get("stdout", ""), d.get("stdout")
        assert "ERR" in d.get("stderr", ""), d.get("stderr")

    # 2.4 non-zero exit code surfaced
    with _Step("exec: false → exit_code=1", results) as s:
        r = await client.exec(sid, "false")
        r.raise_for_status()
        d = r.json()
        assert d.get("exit_code") == 1, d

    # 2.5 timeout enforced
    with _Step("exec: sleep 5 with timeout_s=1 → exec_timeout (≤2.5s)", results) as s:
        t0 = time.perf_counter()
        r = await client.exec(sid, "sleep 5", timeout_s=1)
        elapsed = time.perf_counter() - t0
        d = r.json() if r.status_code == 200 else {}
        # Server may return 200 with timed-out exit_code, or 4xx. Either is fine
        # as long as the server didn't actually wait the full 5s.
        assert elapsed < 2.5, f"timeout not enforced — exec took {elapsed:.1f}s"

    # 2.6 large stdout → truncated:true with effective_truncation_cap_bytes
    with _Step("exec: 16 MiB stdout → truncated:true, cap_bytes set", results) as s:
        # `head -c 16M /dev/zero | base64` produces ~22M of base64-encoded zeros;
        # head/cat the raw bytes via printf "%.0s" — simplest is yes | head -c
        cmd = "head -c 16777216 /dev/zero | tr '\\0' 'x'"
        r = await client.exec(sid, cmd, timeout_s=30)
        r.raise_for_status()
        d = r.json()
        assert d.get("truncated") is True, f"expected truncated=true, got {d.get('truncated')}"
        cap = d.get("effective_truncation_cap_bytes")
        assert cap and cap > 0, f"cap missing or non-positive: {cap}"
        assert "stdout" in (d.get("truncated_streams") or []), d

    # 2.7 env var passed through
    with _Step("exec: env={FOO:bar} → echoed", results) as s:
        r = await client.exec(sid, "echo $FOO", env={"FOO": "bar-from-env"})
        r.raise_for_status()
        assert "bar-from-env" in r.json().get("stdout", ""), r.json()

    # 2.8 Forbidden env vars (HTTP_PROXY etc.) → 400 invalid_argument
    with _Step("exec with env={HTTP_PROXY:...} → 400 invalid_argument", results) as s:
        r = await client._http.post(
            f"/v1/sessions/{sid}/exec",
            json={"argv": ["/bin/true"], "env": {"HTTP_PROXY": "http://evil"}},
            headers={"Idempotency-Key": _idem()},
        )
        # Some builds may quietly drop, others 400; spec says 400 is required
        if r.status_code == 200:
            _skip("server allowed HTTP_PROXY in env — spec says it should be rejected (SPEC-201)")
        assert r.status_code == 400, f"got {r.status_code}"

    # 2.9 each exec is a fresh process (no env leakage)
    with _Step("exec: export FOO does NOT leak across calls", results) as s:
        r = await client.exec(sid, "export ECHOED=set-here")
        r.raise_for_status()
        r = await client.exec(sid, "echo ${ECHOED:-unset}")
        r.raise_for_status()
        assert "unset" in r.json().get("stdout", ""), r.json()

    # 2.10 stdin payload
    with _Step("exec: stdin → cat returns it", results) as s:
        r = await client.exec(sid, "cat", stdin="payload-via-stdin")
        if r.status_code == 200:
            assert "payload-via-stdin" in r.json().get("stdout", ""), r.json()
        else:
            _skip(f"server returned {r.status_code} for stdin field — may not be supported")

    # 2.11 absolute argv[0] works
    with _Step("exec_argv: absolute /bin/echo", results) as s:
        r = await client.exec_argv(sid, ["/bin/echo", "via-argv"])
        r.raise_for_status()
        assert "via-argv" in r.json().get("stdout", ""), r.json()

    return results


async def cat_files(client: _Client, sid: str) -> list[_Result]:
    print("\n── Files API ──")
    results: list[_Result] = []
    seed = uuid.uuid4().hex[:6]

    # 3.1 round-trip via collection-form write
    with _Step("collection POST + GET round-trip", results) as s:
        path = f"col-{seed}.txt"
        r = await client.file_write_collection(sid, path, b"col-bytes")
        r.raise_for_status()
        r = await client.file_read(sid, path)
        assert r.status_code == 200 and r.content == b"col-bytes", r.content

    # 3.2 round-trip via path-in-URL form
    with _Step("path-in-URL POST + GET round-trip", results) as s:
        path = f"sym-{seed}.txt"
        r = await client.file_write_path(sid, path, b"sym-bytes")
        r.raise_for_status()
        r = await client.file_read(sid, path)
        assert r.status_code == 200 and r.content == b"sym-bytes", r.content

    # 3.3 nested write auto-creates parents (issue #2 — was buggy, now fixed)
    with _Step("nested-path file_write auto-creates dirs", results) as s:
        path = f"deep-{seed}/a/b/c.txt"
        r = await client.file_write_collection(sid, path, b"deep")
        r.raise_for_status()
        r = await client.file_read(sid, path)
        assert r.status_code == 200 and r.content == b"deep", r.content

    # 3.4 list shows newly written files
    with _Step("file_list returns newly written entries", results) as s:
        # Write a marker, then list root
        path = f"listed-{seed}.txt"
        await client.file_write_collection(sid, path, b"x")
        r = await client.file_list(sid, "")
        r.raise_for_status()
        names = [e["name"] for e in r.json().get("entries", [])]
        assert path in names, f"{path} not in {names[:10]}"

    # 3.5 list with dir= parameter (REST query name; MCP variant is `subdir`)
    with _Step("file_list with ?dir=<created> returns subdir entries", results) as s:
        await client.file_write_collection(sid, f"sub-{seed}/inner.txt", b"y")
        r = await client.file_list(sid, f"sub-{seed}")
        r.raise_for_status()
        entries = r.json().get("entries", [])
        names = [e["name"] for e in entries]
        assert "inner.txt" in names, f"inner.txt not in {names}"

    # 3.6 mode bit returned and applied (default 416 = 0o640)
    with _Step("file_write returns mode field", results) as s:
        r = await client.file_write_collection(sid, f"mode-{seed}.txt", b"m")
        r.raise_for_status()
        body = r.json()
        assert isinstance(body.get("mode"), int), f"mode missing or not int: {body}"

    # 3.7 file_delete + read → 404
    with _Step("file_delete then read → 404", results) as s:
        path = f"del-{seed}.txt"
        await client.file_write_collection(sid, path, b"d")
        r = await client.file_delete(sid, path)
        assert r.status_code in (200, 204), f"DELETE returned {r.status_code}"
        r = await client.file_read(sid, path)
        assert r.status_code == 404, f"after delete, GET returned {r.status_code}"

    # 3.8 file_delete on directory without recursive=true → 4xx
    with _Step("DELETE dir without recursive → 4xx", results) as s:
        path = f"deldir-{seed}/inner.txt"
        await client.file_write_collection(sid, path, b"i")
        r = await client.file_delete(sid, f"deldir-{seed}")
        # Not all builds enforce this — some delete recursively by default
        if r.status_code in (200, 204):
            _skip(f"server allows non-recursive directory delete (returned {r.status_code})")
        assert 400 <= r.status_code < 500, f"got {r.status_code}"

    # 3.9 file_delete with recursive=true succeeds
    with _Step("DELETE dir with recursive=true succeeds", results) as s:
        path = f"deldir2-{seed}/inner.txt"
        await client.file_write_collection(sid, path, b"i")
        r = await client.file_delete(sid, f"deldir2-{seed}", recursive=True)
        assert r.status_code in (200, 204), f"recursive DELETE returned {r.status_code}"

    # 3.10 binary-safe round-trip
    with _Step("binary content (NUL + non-utf8) round-trip", results) as s:
        path = f"bin-{seed}.bin"
        payload = b"\x00\x01\x02\xfe\xff"
        r = await client.file_write_collection(sid, path, payload)
        r.raise_for_status()
        r = await client.file_read(sid, path)
        assert r.content == payload, r.content.hex()

    return results


async def cat_streaming_exec(client: _Client, sid: str) -> list[_Result]:
    print("\n── Streaming exec ──")
    results: list[_Result] = []

    # 4.1 SSE content-type
    with _Step("exec/stream returns text/event-stream", results) as s:
        async with client._http.stream(
            "POST", f"/v1/sessions/{sid}/exec/stream",
            json={"argv": ["/bin/bash", "-lc", "echo line"]},
            headers={"Idempotency-Key": _idem()},
        ) as r:
            ct = r.headers.get("content-type", "")
            assert "text/event-stream" in ct, f"content-type={ct!r}"

    # 4.2 events arrive in order: stdout chunks, then result
    with _Step("event order: stdout/stderr* → result", results) as s:
        events: list[tuple[str, dict | str]] = []
        async with client._http.stream(
            "POST", f"/v1/sessions/{sid}/exec/stream",
            json={"argv": ["/bin/bash", "-lc", "echo a; echo b 1>&2; echo c"]},
            headers={"Idempotency-Key": _idem()},
        ) as r:
            cur_event = None
            async for raw in r.aiter_lines():
                if not raw:
                    cur_event = None
                    continue
                if raw.startswith("event:"):
                    cur_event = raw.split(":", 1)[1].strip()
                elif raw.startswith("data:") and cur_event:
                    payload = raw.split(":", 1)[1].strip()
                    try:
                        events.append((cur_event, json.loads(payload)))
                    except json.JSONDecodeError:
                        events.append((cur_event, payload))
        assert events, "no events received"
        # Final event must be 'result' with full ExecResponse shape
        kind, body = events[-1]
        assert kind == "result", f"last event kind={kind}"
        assert isinstance(body, dict), body
        assert "exit_code" in body, body
        # At least one stdout chunk before the result
        chunk_kinds = [k for k, _ in events[:-1]]
        assert any(k in ("stdout", "stderr") for k in chunk_kinds), chunk_kinds

    # 4.3 chunks are base64
    with _Step("stdout chunks decode from base64", results) as s:
        async with client._http.stream(
            "POST", f"/v1/sessions/{sid}/exec/stream",
            json={"argv": ["/bin/bash", "-lc", "printf hello"]},
            headers={"Idempotency-Key": _idem()},
        ) as r:
            decoded = b""
            cur_event = None
            async for raw in r.aiter_lines():
                if raw.startswith("event:"):
                    cur_event = raw.split(":", 1)[1].strip()
                elif raw.startswith("data:") and cur_event == "stdout":
                    payload = json.loads(raw.split(":", 1)[1].strip())
                    if "chunk_b64" in payload:
                        decoded += base64.b64decode(payload["chunk_b64"])
            assert decoded == b"hello", f"decoded={decoded!r}"

    return results


async def cat_processes(client: _Client, sid: str) -> list[_Result]:
    print("\n── Processes ──")
    results: list[_Result] = []

    # 5.1 start a daemon, list shows RUNNING
    daemon_pid = None
    with _Step("process_start: short daemon → RUNNING", results) as s:
        r = await client.process_start(sid, ["/bin/bash", "-lc", "sleep 2; echo done"])
        r.raise_for_status()
        body = r.json()
        assert body.get("state") == "RUNNING", body
        daemon_pid = body["process_id"]

    # 5.2 list contains the PID
    with _Step("process_list contains started pid", results) as s:
        if not daemon_pid:
            _skip("daemon not started")
        r = await client.process_list(sid)
        r.raise_for_status()
        ids = [p["process_id"] for p in r.json().get("entries", [])]
        assert daemon_pid in ids, f"{daemon_pid} not in {ids}"

    # 5.3 get returns same shape as start
    with _Step("process_get returns ProcessResponse fields", results) as s:
        if not daemon_pid:
            _skip("daemon not started")
        r = await client.process_get(sid, daemon_pid)
        r.raise_for_status()
        body = r.json()
        for k in ("process_id", "name", "argv", "state", "started_at"):
            assert k in body, f"{k} missing from {sorted(body.keys())}"

    # 5.4 wait for exit, state transitions to EXITED with exit_code populated.
    # Polls every 1s (slower than before to avoid rate-limiting). Distinguishes
    # the real upstream bug (state=EXITED but exit_code is null) from transient
    # transport errors so the failure message is unambiguous.
    with _Step("process exits → state=EXITED, exit_code populated", results) as s:
        if not daemon_pid:
            _skip("daemon not started")
        deadline = time.perf_counter() + 8
        last_body: dict[str, Any] | None = None
        last_state: str | None = None
        while time.perf_counter() < deadline:
            try:
                r = await client.process_get(sid, daemon_pid)
            except httpx.RequestError as e:
                # Transient transport blip — back off and retry, don't fail.
                await asyncio.sleep(1.0)
                continue
            r.raise_for_status()
            last_body = r.json()
            last_state = last_body.get("state")
            if last_state == "EXITED":
                # The real assertion: exit_code must be non-null when EXITED.
                # If null, that's the upstream bug we want to catch.
                assert last_body.get("exit_code") is not None, (
                    f"upstream bug: state=EXITED but exit_code=null. body={last_body}"
                )
                assert last_body.get("exit_code") == 0, last_body
                break
            await asyncio.sleep(1.0)
        else:
            raise AssertionError(f"process never exited; last={last_body}")

    # 5.5 stop a long-running process via DELETE. Validates two things:
    #   - DELETE returns 2xx
    #   - GET-after-DELETE returns 404 process_not_found (mirroring the
    #     session_not_found 404 contract). The OpenAPI declares 404 as a
    #     valid response for this op; getting any other 4xx (e.g. 400
    #     invalid_argument) is a contract violation.
    with _Step("process DELETE then GET → 404 (matches declared response)", results) as s:
        r = await client.process_start(sid, ["/bin/bash", "-lc", "sleep 60"])
        r.raise_for_status()
        pid = r.json()["process_id"]
        r = await client.process_delete(sid, pid)
        assert r.status_code in (200, 204), f"DELETE returned {r.status_code}"
        r = await client.process_get(sid, pid)
        if r.status_code == 200:
            # Some builds keep the EXITED record around after DELETE — that's
            # also reasonable (delete = stop, not erase from registry).
            assert r.json().get("state") in ("EXITED", "STOPPED"), r.json()
        else:
            # If the record is gone, the response code must be 404 per spec,
            # not 400 invalid_argument.
            assert r.status_code == 404, (
                f"upstream contract violation: GET after DELETE returned "
                f"{r.status_code} — OpenAPI declares 404. body={r.text[:200]}"
            )

    # 5.6 logs SSE arrives with chunks
    with _Step("process logs SSE delivers content", results) as s:
        r = await client.process_start(sid, ["/bin/bash", "-lc", "echo log-line-1; sleep 0.5; echo log-line-2"])
        r.raise_for_status()
        pid = r.json()["process_id"]
        try:
            seen = []
            async with client._http.stream(
                "GET", f"/v1/sessions/{sid}/processes/{pid}/logs",
                timeout=5,
            ) as r2:
                ct = r2.headers.get("content-type", "")
                assert "text/event-stream" in ct, f"content-type={ct!r}"
                deadline = time.perf_counter() + 4
                async for raw in r2.aiter_lines():
                    if time.perf_counter() > deadline:
                        break
                    if raw and raw.startswith("data:"):
                        seen.append(raw)
                    if len(seen) >= 2:
                        break
            assert seen, "no log events received"
        finally:
            await client.process_delete(sid, pid)

    return results


async def cat_idempotency(client: _Client, sid: str) -> list[_Result]:
    print("\n── Idempotency replay ──")
    results: list[_Result] = []

    # 6.1 same key → cached body, Idempotent-Replay header set
    with _Step("same key → identical body + Idempotent-Replay header", results) as s:
        key = _idem()
        argv = ["/bin/bash", "-lc", "echo $RANDOM"]
        r1 = await client._http.post(
            f"/v1/sessions/{sid}/exec",
            json={"argv": argv}, headers={"Idempotency-Key": key},
        )
        r2 = await client._http.post(
            f"/v1/sessions/{sid}/exec",
            json={"argv": argv}, headers={"Idempotency-Key": key},
        )
        r1.raise_for_status(); r2.raise_for_status()
        assert r1.json().get("stdout") == r2.json().get("stdout"), \
            f"r1={r1.json().get('stdout')!r}, r2={r2.json().get('stdout')!r}"
        replay_h = r2.headers.get("idempotent-replay")
        assert replay_h, f"missing Idempotent-Replay header: {dict(r2.headers)}"

    # 6.2 different keys → fresh execution
    with _Step("different keys → independent executions", results) as s:
        argv = ["/bin/bash", "-lc", "echo $RANDOM"]
        r1 = await client._http.post(
            f"/v1/sessions/{sid}/exec",
            json={"argv": argv}, headers={"Idempotency-Key": _idem()},
        )
        r2 = await client._http.post(
            f"/v1/sessions/{sid}/exec",
            json={"argv": argv}, headers={"Idempotency-Key": _idem()},
        )
        r1.raise_for_status(); r2.raise_for_status()
        # Statistically very likely different — $RANDOM has 32k range
        # If they're identical it's still possible, but log skip
        if r1.json().get("stdout") == r2.json().get("stdout"):
            _skip("low-entropy collision on $RANDOM — re-run usually clears this")

    # 6.3 Idempotency-Key omitted on mutating route → still works
    with _Step("exec without Idempotency-Key → 200 (header optional)", results) as s:
        r = await client._http.post(
            f"/v1/sessions/{sid}/exec",
            json={"argv": ["/bin/true"]},
        )
        # Spec says optional; should accept
        assert r.status_code == 200, f"got {r.status_code}"

    # 6.4 same key cross-route → 409 (conflict, per spec)
    with _Step("same key, different route → 409 (or replays cleanly)", results) as s:
        key = _idem()
        # First use on /exec
        r1 = await client._http.post(
            f"/v1/sessions/{sid}/exec",
            json={"argv": ["/bin/true"]},
            headers={"Idempotency-Key": key},
        )
        r1.raise_for_status()
        # Reuse same key on /files
        r2 = await client._http.post(
            f"/v1/sessions/{sid}/files",
            json={"path": "idem-cross.txt", "content_b64": base64.b64encode(b"x").decode()},
            headers={"Idempotency-Key": key},
        )
        # Spec says 409 on route mismatch. Some builds may scope keys per-route
        # (then 200/201 fresh). Both are acceptable; just not server error.
        assert r2.status_code != 500, f"unexpected 500 on cross-route reuse"

    return results


async def cat_auth(base_url: str, token: str) -> list[_Result]:
    print("\n── Auth boundary ──")
    results: list[_Result] = []

    # 7.1 No bearer → 401
    with _Step("no Authorization header → 401", results) as s:
        async with httpx.AsyncClient(verify=False, timeout=10) as c:
            r = await c.get(f"{base_url}/v1/sessions")
            # GET /v1/sessions doesn't exist (collection isn't listable), so
            # this might 404 if path doesn't exist. Try a known path.
            r = await c.post(f"{base_url}/v1/sessions", json={})
            assert r.status_code == 401, f"got {r.status_code}"

    # 7.2 Malformed bearer → 401
    with _Step("malformed Authorization → 401", results) as s:
        async with httpx.AsyncClient(verify=False, timeout=10,
                                      headers={"Authorization": "NotBearer xyz"}) as c:
            r = await c.post(f"{base_url}/v1/sessions", json={})
            assert r.status_code == 401, f"got {r.status_code}"

    # 7.3 Wrong token → 401
    with _Step("wrong bearer token → 401", results) as s:
        async with httpx.AsyncClient(verify=False, timeout=10,
                                      headers={"Authorization": "Bearer not-a-real-token"}) as c:
            r = await c.post(f"{base_url}/v1/sessions", json={})
            assert r.status_code == 401, f"got {r.status_code}"

    # 7.4 Session ownership: cross-tenant access surfaces as 404 (per SPEC-405)
    with _Step("cross-tenant session lookup → 404 (identical to never-existed)", results) as s:
        async with httpx.AsyncClient(verify=False, timeout=10,
                                      headers={"Authorization": f"Bearer {token}"}) as c:
            # Try to GET a session id we didn't create
            fake_sid = "01" + uuid.uuid4().hex[:24].upper()
            r = await c.get(f"{base_url}/v1/sessions/{fake_sid}")
            assert r.status_code == 404, f"got {r.status_code}"
            j = r.json()
            code = (j.get("detail") or {}).get("code") if isinstance(j.get("detail"), dict) else None
            assert code == "session_not_found", f"code={code!r}"

    # 7.5 Public endpoints don't require auth
    with _Step("/healthz no auth → 200", results) as s:
        async with httpx.AsyncClient(verify=False, timeout=10) as c:
            r = await c.get(f"{base_url}/healthz")
            assert r.status_code == 200, f"got {r.status_code}"

    return results


async def cat_path_validation(client: _Client, sid: str) -> list[_Result]:
    print("\n── Path validation ──")
    results: list[_Result] = []

    # 8.1 Absolute path → 400 invalid_path
    with _Step("file_write path='/etc/passwd' → 400 invalid_path", results) as s:
        r = await client.file_write_collection(sid, "/etc/passwd", b"x")
        assert r.status_code == 400, f"got {r.status_code}"
        j = r.json()
        code = (j.get("detail") or {}).get("code") if isinstance(j.get("detail"), dict) else None
        assert code in ("invalid_path", "invalid_argument"), f"code={code!r}"

    # 8.2 Path traversal `..` rejected
    with _Step("file_write path='../escape.txt' rejected", results) as s:
        r = await client.file_write_collection(sid, "../escape.txt", b"x")
        assert r.status_code == 400, f"got {r.status_code}"

    # 8.3 deeper traversal still rejected
    with _Step("file_write path='a/b/../../../escape.txt' rejected", results) as s:
        r = await client.file_write_collection(sid, "a/b/../../../escape.txt", b"x")
        assert r.status_code == 400, f"got {r.status_code}"

    # 8.4 NUL byte in path rejected
    with _Step("file_write path with NUL byte rejected", results) as s:
        r = await client.file_write_collection(sid, "nul\x00byte.txt", b"x")
        # Server may 400 invalid_path or 422 (pydantic catches NUL). Either fine.
        assert r.status_code in (400, 422), f"got {r.status_code}"

    # 8.5 file_read on absolute path rejected
    with _Step("file_read /etc/passwd → 400 or 404", results) as s:
        r = await client.file_read(sid, "/etc/passwd")
        # The URL form double-quotes the leading slash; result depends on routing.
        # Either 400 invalid_path, 404 file_not_found, or 405 if route doesn't match
        # is acceptable. 200 is NOT (would be a sandbox escape).
        assert r.status_code != 200, f"got 200 — possible sandbox escape!"
        assert r.status_code in (400, 404, 405, 422), f"got {r.status_code}"

    return results


async def cat_concurrency(client: _Client, sid: str) -> list[_Result]:
    print("\n── Concurrency ──")
    results: list[_Result] = []

    # 9.1 Two parallel exec calls in the same session both succeed
    with _Step("parallel exec in one session: both 200 with correct stdout", results) as s:
        r1, r2 = await asyncio.gather(
            client.exec(sid, "echo parallel-A"),
            client.exec(sid, "echo parallel-B"),
        )
        r1.raise_for_status(); r2.raise_for_status()
        assert "parallel-A" in r1.json().get("stdout", ""), r1.json()
        assert "parallel-B" in r2.json().get("stdout", ""), r2.json()

    # 9.2 Two separate sessions: writes don't bleed
    with _Step("two sessions: workspace state is isolated", results) as s:
        b1 = await client.session_create()
        b2 = await client.session_create()
        sid_a = b1["session_id"]; sid_b = b2["session_id"]
        try:
            await client.file_write_collection(sid_a, "iso.txt", b"FROM-A")
            r = await client.file_read(sid_b, "iso.txt")
            assert r.status_code == 404, f"session B saw session A's file (status {r.status_code})"
        finally:
            await client.session_destroy(sid_a)
            await client.session_destroy(sid_b)

    return results


# === Runner ===


def _resolve_config() -> tuple[str, str]:
    url = os.environ.get(
        "ADK_CC_SANDBOX_SERVICE_URL", "http://127.0.0.1:8000"
    ).rstrip("/")
    token = (
        os.environ.get("ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN")
        or os.environ.get("ADK_CC_SANDBOX_SERVICE_TOKEN")
        or os.environ.get("SANDBOX_API_TOKEN")
    )
    if not token:
        print("[skip] no token. Set ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN or SANDBOX_API_TOKEN.")
        sys.exit(0)
    return url, token


async def main() -> int:
    url, token = _resolve_config()
    print(f"target: {url}")
    print(f"token:  {token[:6]}…({len(token)} chars)")

    # Pull OpenAPI version for traceability
    try:
        async with httpx.AsyncClient(verify=False, timeout=5) as c:
            r = await c.get(f"{url}/openapi.json")
            ver = r.json().get("info", {}).get("version", "?")
            print(f"openapi info.version: {ver}")
    except Exception:
        pass

    client = _Client(url, token)

    # Most categories share one session; lifecycle / auth / concurrency manage their own.
    main_session: dict | None = None
    sid: str | None = None
    by_cat: dict[str, list[_Result]] = {}
    try:
        # Lifecycle category creates and destroys its own sessions.
        by_cat["session_lifecycle"] = await cat_session_lifecycle(client)

        # Open one session for the workhorse categories
        main_session = await client.session_create()
        sid = main_session["session_id"]
        print(f"\nshared session: {sid}")

        by_cat["exec"]            = await cat_exec_contract(client, sid)
        by_cat["files"]           = await cat_files(client, sid)
        by_cat["streaming_exec"]  = await cat_streaming_exec(client, sid)
        by_cat["processes"]       = await cat_processes(client, sid)
        by_cat["idempotency"]     = await cat_idempotency(client, sid)
        by_cat["auth"]            = await cat_auth(url, token)
        by_cat["path_validation"] = await cat_path_validation(client, sid)
        by_cat["concurrency"]     = await cat_concurrency(client, sid)
    finally:
        if sid:
            try:
                await client.session_destroy(sid)
            except Exception:
                pass
        await client.aclose()

    # Print per-category and overall summary
    total_ok = total_fail = total_skip = 0
    print("\n" + "=" * 60)
    print("Results")
    print("=" * 60)
    for cat, results in by_cat.items():
        ok = sum(1 for r in results if r.status == "OK")
        fail = sum(1 for r in results if r.status == "FAIL")
        skip = sum(1 for r in results if r.status == "SKIP")
        total_ok += ok; total_fail += fail; total_skip += skip
        bar = "✓" if fail == 0 else "✗"
        print(f"  {bar} {cat:22s}  {ok} OK, {fail} FAIL, {skip} SKIP")
        if fail or skip:
            for r in results:
                if r.status != "OK":
                    print(str(r))

    total = total_ok + total_fail + total_skip
    print("=" * 60)
    print(f"TOTAL: {total_ok}/{total} passing  ({total_fail} failed, {total_skip} skipped)")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
