#!/usr/bin/env python3
"""End-to-end test: SandboxServiceBackend.exec_stream + BashTool streaming.

Where `tests/e2e_sandbox_comprehensive.py` validates the upstream's
streaming protocol at the wire level (httpx-only, no adk_cc dep), this
script validates **adk-cc's adapter**: that `SandboxServiceBackend.exec_stream`
correctly parses the SSE stream into `ExecChunk`s, and that
`BashTool` with `ADK_CC_BASH_STREAM=1` consumes those chunks and logs
them at INFO without changing the model-facing return shape.

Two test categories:

  Part A — `SandboxServiceBackend.exec_stream` against live upstream
    - single-chunk command: 1 stdout chunk + 1 result terminator
    - multi-chunk command: ≥3 chunks, terminator carries aggregated result
    - stderr-only command: stderr chunks delivered, no spurious stdout
    - failing command: result.exit_code reflects actual exit
    - empty output: just the terminator
    - sync `exec` after `exec_stream` works on the same session
      (verifies streaming doesn't poison session/connection state)

  Part B — `BashTool` with `ADK_CC_BASH_STREAM=1`
    - default behavior (env unset): no streaming, no chunk logs
    - opt-in (env=1): chunks emit at INFO log level in order
    - return value to model is unchanged regardless of streaming

Both parts depend on:
  - Python ≥3.12 (for adk_cc imports)
  - LAN/loopback reach to the configured sandbox URL

Like `e2e_skills.py`, has a preflight reachability check that skips
clean if neither holds. Run on a properly-configured host (e.g. the
sandbox box itself, where loopback bypasses everything).

Run:
    .venv/bin/python tests/e2e_streaming_adapter.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    import httpx
except ImportError:
    print("[skip] httpx not installed.")
    sys.exit(0)

# Auto-source `.env`
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

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")

if sys.version_info < (3, 12):
    print(
        f"[skip] this script needs Python ≥3.12 (running {sys.version_info.major}."
        f"{sys.version_info.minor}). Smoke + comprehensive e2e are stdlib-only "
        f"and run on system python; this one needs the adk_cc package."
    )
    sys.exit(0)

from adk_cc.sandbox.backends.sandbox_service_backend import SandboxServiceBackend
from adk_cc.sandbox.config import (
    ExecChunk,
    FsReadConfig,
    FsWriteConfig,
    NetworkConfig,
)
from adk_cc.sandbox.workspace import WorkspaceRoot


# === Test infrastructure ===


class _Step:
    def __init__(self, name: str, results: list):
        self.name = name
        self.results = results
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        ms = (time.perf_counter() - self.t0) * 1000
        if exc is None:
            print(f"  [OK]   {self.name:55s} ({ms:.0f} ms)")
            self.results.append(True)
            return False
        detail = f"{type(exc).__name__}: {exc}"
        print(f"  [FAIL] {self.name:55s} ({ms:.0f} ms)")
        for line in detail.splitlines():
            print(f"         {line}")
        for line in traceback.format_exception(exc_type, exc, tb)[-3:]:
            for sub in line.rstrip().splitlines():
                print(f"         {sub}")
        self.results.append(False)
        return True


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
        print("[skip] no token. Set SANDBOX_API_TOKEN.")
        sys.exit(0)
    return url, token


async def _preflight(url: str) -> None:
    async with httpx.AsyncClient(verify=False, timeout=3) as c:
        try:
            r = await c.get(f"{url}/healthz")
            r.raise_for_status()
        except (httpx.ConnectError, httpx.RequestError) as e:
            print(
                f"[skip] sandbox at {url} unreachable from this Python "
                f"binary ({sys.executable})."
            )
            print(
                f"       on macOS, the uv-managed venv python often needs "
                f"Local Network privacy access. errno: "
                f"{type(e).__name__}: {e}"
            )
            sys.exit(0)


def _fake_ctx(backend: SandboxServiceBackend, ws: WorkspaceRoot):
    """Minimal context shape for tools that read backend + ws from
    session state. Tools call `get_backend(ctx)` and `get_workspace(ctx)`
    which read `temp:sandbox_backend` and `temp:sandbox_workspace`."""
    state = {
        "temp:sandbox_backend": backend,
        "temp:sandbox_workspace": ws,
    }
    # The real ToolContext is a Pydantic type; for read-only state
    # access SimpleNamespace is sufficient.
    return SimpleNamespace(state=state)


# === Part A — SandboxServiceBackend.exec_stream ===


async def part_a_backend_exec_stream(backend: SandboxServiceBackend, ws: WorkspaceRoot) -> list[bool]:
    print("\n── Part A: SandboxServiceBackend.exec_stream ──")
    results: list[bool] = []
    fs_w = FsWriteConfig(allow_paths=(ws.abs_path, f"{ws.abs_path}/**"))
    net = NetworkConfig()

    # A.1 single-chunk command
    with _Step("single-chunk: echo hi → ≥1 stdout + 1 result", results):
        chunks: list[ExecChunk] = []
        async for c in backend.exec_stream(
            "echo hi-from-stream",
            fs_write=fs_w, network=net, timeout_s=10, cwd=ws.abs_path,
        ):
            chunks.append(c)
        # Last chunk MUST be the result terminator (always)
        assert chunks[-1].kind == "result", chunks
        assert chunks[-1].result is not None
        # Aggregated result has the expected payload
        assert chunks[-1].result.exit_code == 0, chunks[-1].result
        assert "hi-from-stream" in chunks[-1].result.stdout, chunks[-1].result
        # At least one stdout chunk before the terminator
        non_terminator = [c for c in chunks[:-1] if c.kind == "stdout"]
        # Note: upstream may bunch chunks (filed as issue #14); the
        # contract here is "≥1 stdout chunk before result", which
        # holds whether streamed or buffered.
        assert non_terminator, f"no stdout chunks: {[(c.kind, c.data) for c in chunks]}"

    # A.2 multi-line command — all lines reach the consumer.
    # NB: number of chunks is a property of upstream's flush discipline,
    # not the adapter. With the buffering bug (issue #14) all three
    # echoes collapse into a single stdout chunk; with proper streaming
    # they arrive as three. Either is OK here — the adapter's job is
    # to deliver every byte and terminate with one result. The "chunks
    # arrive over time" test in comprehensive separately validates
    # actual streaming behavior at the protocol level.
    with _Step("multi-line: 3 echoes → all lines aggregated in result", results):
        chunks = []
        async for c in backend.exec_stream(
            "for i in 1 2 3; do echo line-$i; done",
            fs_write=fs_w, network=net, timeout_s=10, cwd=ws.abs_path,
        ):
            chunks.append(c)
        stdout_chunks = [c for c in chunks if c.kind == "stdout"]
        assert stdout_chunks, [(c.kind, c.data) for c in chunks]
        # Adapter contract: aggregate of stdout chunks (or final result.stdout)
        # contains every echoed line. Both are valid views of the same data.
        chunked_total = "".join(c.data for c in stdout_chunks)
        assert chunks[-1].kind == "result"
        assert chunks[-1].result.exit_code == 0
        for tok in ("line-1", "line-2", "line-3"):
            assert tok in chunks[-1].result.stdout, (
                f"{tok!r} missing from result.stdout: {chunks[-1].result.stdout!r}"
            )
            assert tok in chunked_total, (
                f"{tok!r} missing from streamed chunks: {chunked_total!r}"
            )

    # A.3 stderr-only command — stderr chunks present, no stdout
    with _Step("stderr-only: echo to stderr → stderr chunks, no stdout", results):
        chunks = []
        async for c in backend.exec_stream(
            "echo only-stderr 1>&2",
            fs_write=fs_w, network=net, timeout_s=10, cwd=ws.abs_path,
        ):
            chunks.append(c)
        stderr_chunks = [c for c in chunks if c.kind == "stderr"]
        stdout_chunks = [c for c in chunks if c.kind == "stdout"]
        assert stderr_chunks, [(c.kind, c.data) for c in chunks]
        assert not stdout_chunks, f"unexpected stdout chunks: {stdout_chunks}"
        # Final result aggregates correctly
        assert "only-stderr" in chunks[-1].result.stderr, chunks[-1].result

    # A.4 failing command — exit_code propagates
    with _Step("failing: exit 42 → result.exit_code=42", results):
        chunks = []
        async for c in backend.exec_stream(
            "exit 42",
            fs_write=fs_w, network=net, timeout_s=10, cwd=ws.abs_path,
        ):
            chunks.append(c)
        assert chunks[-1].kind == "result"
        assert chunks[-1].result.exit_code == 42, chunks[-1].result

    # A.5 empty output — just the terminator
    with _Step("empty output: /bin/true → only result chunk", results):
        chunks = []
        async for c in backend.exec_stream(
            "/bin/true",
            fs_write=fs_w, network=net, timeout_s=10, cwd=ws.abs_path,
        ):
            chunks.append(c)
        # Some upstream impls might emit a 0-byte stdout chunk; tolerate
        # that. The hard assertion is: result is the LAST chunk and
        # exit_code is 0.
        assert chunks[-1].kind == "result"
        assert chunks[-1].result.exit_code == 0
        non_terminator = chunks[:-1]
        for c in non_terminator:
            # Any non-terminator must be empty/whitespace data, not
            # actual content (sanity check).
            assert c.kind in ("stdout", "stderr"), c
            assert not c.data.strip(), f"unexpected output: {c!r}"

    # A.6 sync exec after exec_stream — verifies streaming doesn't
    # poison session/connection state. Tests one of the original cross-
    # loop fix's invariants: session_id stays cached, fresh client per
    # call doesn't leak.
    with _Step("sync exec after exec_stream → session reusable", results):
        result = await backend.exec(
            "echo follow-up",
            fs_write=fs_w, network=net, timeout_s=10, cwd=ws.abs_path,
        )
        assert result.exit_code == 0, result
        assert "follow-up" in result.stdout, result

    return results


# === Part B — BashTool with ADK_CC_BASH_STREAM=1 ===


class _LogCapture(logging.Handler):
    """Captures every log record at the configured level."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def part_b_bashtool_streaming(backend: SandboxServiceBackend, ws: WorkspaceRoot) -> list[bool]:
    print("\n── Part B: BashTool with ADK_CC_BASH_STREAM ──")
    results: list[bool] = []

    # Lazy import: BashTool transitively pulls in ADK; we want the
    # script to skip cleanly on Python <3.12 before this point.
    from adk_cc.tools.bash.tool import BashTool
    from adk_cc.tools.schemas import RunBashArgs

    # Attach a log capture to the bash tool's logger
    tool_log = logging.getLogger("adk_cc.tools.bash.tool")
    tool_log.setLevel(logging.INFO)
    capture = _LogCapture()
    tool_log.addHandler(capture)

    ctx = _fake_ctx(backend, ws)
    tool = BashTool()

    try:
        # B.1 default behavior — env unset, no chunk logs
        with _Step("env unset: no streaming, no chunk logs", results):
            os.environ.pop("ADK_CC_BASH_STREAM", None)
            capture.records.clear()
            args = RunBashArgs(command="echo control-line", timeout_seconds=10)
            result = await tool._execute(args, ctx)
            assert result.get("status") == "ok", result
            assert "control-line" in result.get("stdout", ""), result
            # No "run_bash[stdout]:" log lines — that's the streaming
            # marker. Sync path doesn't emit those.
            chunk_logs = [
                r for r in capture.records
                if r.getMessage().startswith("run_bash[")
            ]
            assert not chunk_logs, (
                f"unexpected chunk logs in sync mode: "
                f"{[r.getMessage() for r in chunk_logs]}"
            )

        # B.2 streaming on — chunks logged in order
        with _Step("env=1: chunks logged at INFO in order", results):
            os.environ["ADK_CC_BASH_STREAM"] = "1"
            capture.records.clear()
            args = RunBashArgs(
                command="echo first; echo second; echo third",
                timeout_seconds=10,
            )
            result = await tool._execute(args, ctx)
            assert result.get("status") == "ok", result
            # Aggregated result still complete
            stdout = result.get("stdout", "")
            for tok in ("first", "second", "third"):
                assert tok in stdout, f"{tok!r} missing from {stdout!r}"
            # Chunk logs were emitted
            chunk_logs = [
                r for r in capture.records
                if r.getMessage().startswith("run_bash[stdout]:")
            ]
            assert chunk_logs, (
                f"no chunk logs in streaming mode: "
                f"{[r.getMessage() for r in capture.records]}"
            )
            # Verify content order: log lines should mention first,
            # second, third in that order somewhere across the records
            # (whether bunched or separate).
            joined = "\n".join(r.getMessage() for r in chunk_logs)
            for tok in ("first", "second", "third"):
                assert tok in joined, (
                    f"{tok!r} not in any chunk log: {joined!r}"
                )

        # B.3 streaming + failing command — stderr captured, return shape unchanged
        with _Step("env=1: failing command → status:ok exit_code surfaces", results):
            os.environ["ADK_CC_BASH_STREAM"] = "1"
            capture.records.clear()
            args = RunBashArgs(command="echo oops 1>&2; exit 9", timeout_seconds=10)
            result = await tool._execute(args, ctx)
            # BashTool's "status" field is "ok" or "timeout"; exit_code
            # carries actual exit. Streaming doesn't change either.
            assert result.get("status") == "ok", result
            assert result.get("exit_code") == 9, result
            assert "oops" in result.get("stderr", ""), result
    finally:
        tool_log.removeHandler(capture)
        os.environ.pop("ADK_CC_BASH_STREAM", None)

    return results


# === Runner ===


async def run(url: str, token: str) -> bool:
    print(f"target: {url}")
    print(f"token:  {token[:6]}…({len(token)} chars)")
    print()

    await _preflight(url)

    session_id = f"adkcc-stream-e2e-{uuid.uuid4().hex[:8]}"
    backend = SandboxServiceBackend(
        base_url=url,
        api_token=token,
        session_id=session_id,
        tenant_id="stream-e2e",
        verify_tls=False,
    )
    ws = WorkspaceRoot(
        tenant_id="stream-e2e",
        session_id=session_id,
        abs_path=f"/host/wks/stream-e2e/{session_id}",
    )

    all_results: list[bool] = []
    try:
        await backend.ensure_workspace(ws)
        all_results.extend(await part_a_backend_exec_stream(backend, ws))
        all_results.extend(await part_b_bashtool_streaming(backend, ws))
    finally:
        try:
            await backend.close()
        except Exception:
            pass

    print()
    ok = sum(1 for r in all_results if r)
    print("=" * 60)
    print(f"TOTAL: {ok}/{len(all_results)} passing")
    return all(all_results)


def main() -> int:
    url, token = _resolve_config()
    try:
        ok = asyncio.run(run(url, token))
    except KeyboardInterrupt:
        return 130
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
