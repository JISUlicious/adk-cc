#!/usr/bin/env python3
"""Diagnostic for the comprehensive e2e's "chunks arrive over time"
failure on the host. Sandbox team's curl probe shows ~316 ms cadence
between SSE events (proper streaming). Our httpx-based test shows
span=0 (apparent bunching). One of three things is happening:

  1. httpx `aiter_lines()` is internally buffering and yielding lines
     in a burst (bug in the client we'd have to work around)
  2. asyncio scheduling on this host quantises iteration so multiple
     awaits resolve in the same time.perf_counter() tick
  3. The TCP path between this client and the service is coalescing
     small frames before delivery (Nagle / loopback batching)

Print everything timestamped. Compare against curl's 316 ms cadence:
  - If lines arrive ~316 ms apart here → my comprehensive test had a
    bug; fix that
  - If lines arrive bunched here → httpx or transport, not the server

Run on the host:
  ADK_CC_SANDBOX_SERVICE_URL=http://127.0.0.1:8000 \
    SANDBOX_API_TOKEN=<token> \
    .venv/bin/python tests/diag_streaming_timing.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

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


async def _raw_probe(
    url: str,
    token: str,
    sid: str,
    *,
    connection_header: str,
    user_agent: str,
    label: str,
) -> None:
    """Open a raw asyncio TCP socket, send an HTTP/1.1 POST to
    /exec/stream, print body chunks with timestamps. Used to vary
    the request headers to see if the server changes streaming
    behavior based on the client's identity."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    body = (
        '{"argv":["/bin/bash","-lc",'
        '"for i in 1 2 3; do echo line-$i; sleep 0.3; done"]}'
    )
    request = (
        f"POST /v1/sessions/{sid}/exec/stream HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"User-Agent: {user_agent}\r\n"
        f"Accept: */*\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Content-Type: application/json\r\n"
        f"Idempotency-Key: {uuid.uuid4().hex}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: {connection_header}\r\n"
        f"\r\n"
        f"{body}"
    ).encode()

    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request)
    await writer.drain()

    # Capture and print response headers
    print(f"  ── {label} response headers:")
    while True:
        line = await reader.readline()
        if not line or line == b"\r\n":
            break
        decoded = line.decode("ascii", errors="replace").rstrip()
        # Only print interesting headers
        lower = decoded.lower()
        if any(
            tag in lower
            for tag in (
                "content-type", "content-length", "content-encoding",
                "transfer-encoding", "x-accel", "cache-control",
                "connection",
            )
        ):
            print(f"     {decoded}")
        # First line is "HTTP/1.1 200 ..." — print that too
        if decoded.startswith("HTTP/"):
            print(f"     {decoded}")

    print(f"  ── {label} body chunks (post-headers timer):")
    t0 = time.perf_counter()
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(256), timeout=5)
            if not chunk:
                break
            t = time.perf_counter() - t0
            print(f"     {t:.3f}s  +{len(chunk):>4d}B  {chunk[:80]!r}")
    except asyncio.TimeoutError:
        print("     (timeout — connection idle, probably done)")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> int:
    url = os.environ.get(
        "ADK_CC_SANDBOX_SERVICE_URL", "http://127.0.0.1:8000"
    ).rstrip("/")
    token = (
        os.environ.get("ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN")
        or os.environ.get("ADK_CC_SANDBOX_SERVICE_TOKEN")
        or os.environ.get("SANDBOX_API_TOKEN")
    )
    if not token:
        print("[skip] no SANDBOX_API_TOKEN")
        return 0

    print(f"target: {url}")
    print(f"httpx version: {httpx.__version__}")
    print()

    cmd_argv = [
        "/bin/bash", "-lc",
        "for i in 1 2 3; do echo line-$i; sleep 0.3; done",
    ]

    async with httpx.AsyncClient(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
        verify=False,
    ) as client:
        r = await client.post(
            "/v1/sessions", json={},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        r.raise_for_status()
        sid = r.json()["session_id"]
        print(f"session: {sid}")

        # === Probe 1a: headers for SHORT command (instant response) ===
        print()
        print("=== headers — SHORT cmd 'echo immediate' (probe 1a) ===")
        async with client.stream(
            "POST", f"/v1/sessions/{sid}/exec/stream",
            json={"argv": ["/bin/bash", "-lc", "echo immediate"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ) as r:
            for k, v in r.headers.items():
                if k.lower() in (
                    "content-type", "content-encoding",
                    "content-length", "transfer-encoding",
                    "x-accel-buffering", "cache-control",
                    "connection",
                ):
                    print(f"  {k}: {v}")
            async for _ in r.aiter_bytes():
                pass

        # === Probe 1b: headers for SLOW command (multi-event response) ===
        # Critical question: does the server use Transfer-Encoding: chunked
        # for the multi-event response (= streaming) or Content-Length
        # (= buffered)? If Content-Length, the server is buffering this
        # specific path despite the team's curl probe seeing cadence.
        print()
        print("=== headers — SLOW cmd (1s with 3 echoes) (probe 1b) ===")
        async with client.stream(
            "POST", f"/v1/sessions/{sid}/exec/stream",
            json={"argv": cmd_argv},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ) as r:
            for k, v in r.headers.items():
                if k.lower() in (
                    "content-type", "content-encoding",
                    "content-length", "transfer-encoding",
                    "x-accel-buffering", "cache-control",
                    "connection",
                ):
                    print(f"  {k}: {v}")
            async for _ in r.aiter_bytes():
                pass

        # === Probe 2: aiter_raw (skips Content-Encoding decoding) ===
        # If the server sends gzipped/deflate, aiter_bytes may buffer
        # to decompress while aiter_raw streams. If raw streams but
        # bytes don't, that's the hint.
        print()
        print("=== aiter_raw (no Content-Encoding decoding) ===")
        t0 = time.perf_counter()
        async with client.stream(
            "POST", f"/v1/sessions/{sid}/exec/stream",
            json={"argv": cmd_argv},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ) as r:
            async for raw in r.aiter_raw():
                t = time.perf_counter() - t0
                print(f"  {t:.3f}s  +{len(raw):>4d}B  {raw[:120]!r}")

        # === Probe 3: aiter_bytes with explicit small chunk_size ===
        # Default chunk_size may pull large batches from the transport.
        # Force chunk_size=1 — we should see one byte at a time IF
        # the transport is delivering as data arrives.
        print()
        print("=== aiter_bytes(chunk_size=1) ===")
        t0 = time.perf_counter()
        async with client.stream(
            "POST", f"/v1/sessions/{sid}/exec/stream",
            json={"argv": cmd_argv},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ) as r:
            chunk_count = 0
            first_byte_t: float | None = None
            last_byte_t: float | None = None
            async for raw in r.aiter_bytes(chunk_size=1):
                t = time.perf_counter() - t0
                if first_byte_t is None:
                    first_byte_t = t
                last_byte_t = t
                chunk_count += 1
            print(
                f"  chunks={chunk_count}  "
                f"first={first_byte_t:.3f}s  last={last_byte_t:.3f}s  "
                f"span={(last_byte_t - first_byte_t):.3f}s"
            )

        # === Probe 4a: Raw asyncio socket with httpx-like headers ===
        # Bypass httpx but reproduce its header set. If THIS streams,
        # the buffering is in httpx. If it bunches, it's the server
        # responding to one of these headers.
        print()
        print("=== raw asyncio socket (httpx-like headers) ===")
        await _raw_probe(
            url, token, sid,
            connection_header="close",
            user_agent="python-httpx/0.28.1",
            label="httpx-like",
        )

        # === Probe 4b: Raw socket with CURL-like headers ===
        # Curl sends `Connection: keep-alive` (default), `Accept: */*`,
        # and `User-Agent: curl/...`. If the server fast-paths streaming
        # for clients that look like curl, we'd see streaming here but
        # not in 4a.
        print()
        print("=== raw asyncio socket (curl-like headers) ===")
        await _raw_probe(
            url, token, sid,
            connection_header="keep-alive",
            user_agent="curl/8.0.0",
            label="curl-like",
        )

        # Cleanup
        await client.post(
            f"/v1/sessions/{sid}/stop",
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
