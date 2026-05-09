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

    async with httpx.AsyncClient(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
        verify=False,
    ) as client:
        # Create session
        r = await client.post(
            "/v1/sessions", json={},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        r.raise_for_status()
        sid = r.json()["session_id"]
        print(f"session: {sid}")

        # Drive the same probe the comprehensive test uses, with rich
        # timestamping at three layers:
        #   - aiter_bytes: raw byte chunks from transport
        #   - aiter_lines: parsed lines
        #   - per-line decode/print
        print()
        print("=== aiter_bytes (raw transport chunks) ===")
        t0 = time.perf_counter()
        try:
            async with client.stream(
                "POST", f"/v1/sessions/{sid}/exec/stream",
                json={
                    "argv": [
                        "/bin/bash", "-lc",
                        "for i in 1 2 3; do echo line-$i; sleep 0.3; done",
                    ],
                },
                headers={"Idempotency-Key": uuid.uuid4().hex},
            ) as r:
                async for raw_bytes in r.aiter_bytes():
                    t = time.perf_counter() - t0
                    print(
                        f"  {t:.3f}s  +{len(raw_bytes):>4d}B  "
                        f"{raw_bytes[:120]!r}"
                    )
        except Exception as e:
            print(f"  err: {type(e).__name__}: {e}")

        print()
        print("=== aiter_lines (line decoder) ===")
        t0 = time.perf_counter()
        try:
            async with client.stream(
                "POST", f"/v1/sessions/{sid}/exec/stream",
                json={
                    "argv": [
                        "/bin/bash", "-lc",
                        "for i in 1 2 3; do echo line-$i; sleep 0.3; done",
                    ],
                },
                headers={"Idempotency-Key": uuid.uuid4().hex},
            ) as r:
                async for line in r.aiter_lines():
                    t = time.perf_counter() - t0
                    print(f"  {t:.3f}s  {line[:100]!r}")
        except Exception as e:
            print(f"  err: {type(e).__name__}: {e}")

        # Cleanup
        await client.post(
            f"/v1/sessions/{sid}/stop",
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
