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

        # === Probe 1: Inspect response headers ===
        # If Content-Encoding/Transfer-Encoding is present, httpx may
        # be buffering for decode. We never want to see Content-Length
        # on a streaming response.
        print()
        print("=== response headers (probe 1) ===")
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
            # Drain
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

        # === Probe 4: Raw asyncio socket (no httpx) ===
        # Bypass httpx entirely. Talk HTTP/1.1 directly via asyncio
        # streams. If THIS streams cleanly but httpx doesn't, the
        # buffering is unambiguously inside httpx.
        print()
        print("=== raw asyncio socket (no httpx) ===")
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
            f"Authorization: Bearer {token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Idempotency-Key: {uuid.uuid4().hex}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        ).encode()

        reader, writer = await asyncio.open_connection(host, port)
        writer.write(request)
        await writer.drain()

        # Skip headers (everything until \r\n\r\n)
        while True:
            line = await reader.readline()
            if not line or line == b"\r\n":
                break

        t0 = time.perf_counter()
        try:
            while True:
                # Read in tiny chunks — minimal client buffering
                chunk = await asyncio.wait_for(reader.read(256), timeout=5)
                if not chunk:
                    break
                t = time.perf_counter() - t0
                print(f"  {t:.3f}s  +{len(chunk):>4d}B  {chunk[:120]!r}")
        except asyncio.TimeoutError:
            print("  (timeout — connection idle, probably done)")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        # Cleanup
        await client.post(
            f"/v1/sessions/{sid}/stop",
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
