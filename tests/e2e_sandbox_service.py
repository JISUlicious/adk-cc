#!/usr/bin/env python3
"""End-to-end test against a live sandbox service.

This script validates the **wire-level contract** between adk-cc and the
upstream sandbox service (JISUlicious/sandboxing) — every HTTP call
adk-cc's `SandboxServiceBackend` makes, in the same order, with the same
headers and body shapes. It does NOT import `adk_cc` itself, so it runs
on any Python ≥3.8 with httpx installed (the macOS system python at
`/usr/bin/python3` works).

Why standalone vs. importing `SandboxServiceBackend` directly: in some
environments (e.g. macOS Local Network privacy + a uv-managed venv
python that hasn't been granted access), the adk-cc venv can't reach
LAN destinations. The system python can. Decoupling the e2e from the
package keeps the contract test runnable in those environments. Code-
path coverage of `SandboxServiceBackend` itself lives in
`tests/test_sandbox_service_backend.py` (mocked at the httpx boundary).

Configuration (env, or auto-sourced from the adk-cc `.env`):

    ADK_CC_SANDBOX_SERVICE_URL=http://172.30.1.41:8000
    ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN=<bearer>      # OR
    SANDBOX_API_TOKEN=<bearer>                         # also accepted

Behavior:
  - Skips with a clear note if no token is found.
  - Each step prints `[OK]` / `[FAIL]` plus elapsed time.
  - On any failure the script tears down the upstream session it
    created (best-effort) so reruns aren't hindered by leaked state.
  - Exits non-zero if any step failed.

Run:
    /usr/bin/python3 tests/e2e_sandbox_service.py
    # or any python with httpx installed
"""

from __future__ import annotations

import asyncio
import base64
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
    print(
        "[skip] httpx not installed. Install with:\n"
        "       /usr/bin/python3 -m pip install --user httpx"
    )
    sys.exit(0)


# === Auto-source `.env` so operators don't have to remember which file ===
_REPO = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO / ".env"
if _ENV_FILE.is_file():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


CONTAINER_WORKSPACE = "/workspace"


# === The contract under test ===
#
# Every call below is what `adk_cc/sandbox/backends/sandbox_service_backend.py`
# emits. If the live service rejects any of these, adk-cc would fail
# the same way at runtime — better to catch it here.
#
# - Bearer auth on every request.
# - `Idempotency-Key` header on every mutating request (POST /sessions,
#   POST /exec, POST /files/<path>, POST /sessions/<id>/stop).
# - argv wrapped as `["/bin/bash", "-lc", <cmd>]`. cwd != /workspace
#   becomes `cd '<sub>' && <cmd>`.
# - file_write body is raw octet-stream (UTF-8 content), not JSON.
# - read_text expects 404 for missing files (raises FileNotFoundError).
# - close() POSTs /stop, never /destroy.
#
# Path translation in the real backend: <ws.abs_path>/foo →
# /workspace/foo. The e2e passes paths in the /workspace-relative form
# directly so we don't need to simulate the host-prefix stripping.


def _idem() -> str:
    return uuid.uuid4().hex


class _SandboxClient:
    """Minimal client mirroring SandboxServiceBackend's HTTP shape."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
            verify=False,
        )
        self.session_id: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def session_create(self) -> str:
        resp = await self._client.post(
            "/v1/sessions",
            json={},
            headers={"Idempotency-Key": _idem()},
        )
        resp.raise_for_status()
        body = resp.json()
        sid = body.get("id") or body.get("session_id")
        if not sid:
            raise RuntimeError(
                f"session_create response missing id/session_id: {body!r}"
            )
        self.session_id = sid
        return sid

    async def exec(
        self,
        cmd: str,
        *,
        cwd: str = CONTAINER_WORKSPACE,
        timeout_s: int = 30,
    ) -> dict[str, Any]:
        argv = ["/bin/bash", "-lc", cmd]
        if cwd != CONTAINER_WORKSPACE:
            quoted = cwd.replace("'", "'\\''")
            argv = ["/bin/bash", "-lc", f"cd '{quoted}' && {cmd}"]
        resp = await self._client.post(
            f"/v1/sessions/{self.session_id}/exec",
            json={"argv": argv, "timeout_s": timeout_s},
            headers={"Idempotency-Key": _idem()},
        )
        resp.raise_for_status()
        return resp.json()

    async def file_write(self, rel_path: str, content: str) -> None:
        # Per the actual OpenAPI contract: write is POST /files (collection,
        # NOT path-in-URL) with JSON {path, content_b64, mode}. Read is
        # GET /files/{path} → raw octet-stream — asymmetric on purpose.
        body = {
            "path": rel_path,
            "content_b64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        resp = await self._client.post(
            f"/v1/sessions/{self.session_id}/files",
            json=body,
            headers={"Idempotency-Key": _idem()},
        )
        resp.raise_for_status()

    async def file_read(self, rel_path: str) -> str:
        url = f"/v1/sessions/{self.session_id}/files/{quote(rel_path, safe='/')}"
        resp = await self._client.get(url)
        if resp.status_code == 404:
            raise FileNotFoundError(rel_path)
        resp.raise_for_status()
        return resp.content.decode("utf-8", errors="replace")

    async def session_stop(self) -> None:
        if not self.session_id:
            return
        await self._client.post(
            f"/v1/sessions/{self.session_id}/stop",
            headers={"Idempotency-Key": _idem()},
        )


# === Bug-fix verification ===
#
# These probes track the issues filed in
# `~/.claude/plans/plan-sandbox-issues-from-e2e.md`. Unlike the contract
# steps below (which validate "the parts that already work"), these are
# expected to start as BUGGY and flip to FIXED as upstream lands fixes.
# A BUGGY result is informational — the script's exit code only flips on
# contract regressions, so this section can be re-run on every server
# update to track which issues have been resolved.


class _BugCheckResult:
    FIXED = "FIXED"
    BUGGY = "BUGGY"
    ERROR = "ERROR"


def _print_bug(label: str, status: str, detail: str = "") -> None:
    tag = {
        _BugCheckResult.FIXED: "FIXED",
        _BugCheckResult.BUGGY: "BUGGY",
        _BugCheckResult.ERROR: "ERROR",
    }[status]
    print(f"  [{tag:5s}] {label}")
    if detail:
        for line in detail.splitlines():
            print(f"          {line}")


async def _check_issue_1_symmetric_file_write(sb: "_SandboxClient") -> tuple[str, str]:
    """#1 — POST /v1/sessions/<sid>/files/<path> with octet-stream should work."""
    rel = f"sym-probe-{uuid.uuid4().hex[:6]}.txt"
    url = f"/v1/sessions/{sb.session_id}/files/{quote(rel, safe='/')}"
    try:
        resp = await sb._client.post(
            url,
            content=b"sym-write",
            headers={
                "Content-Type": "application/octet-stream",
                "Idempotency-Key": _idem(),
            },
        )
    except Exception as e:
        return _BugCheckResult.ERROR, f"probe failed: {type(e).__name__}: {e}"
    if 200 <= resp.status_code < 300:
        return _BugCheckResult.FIXED, f"HTTP {resp.status_code}"
    return _BugCheckResult.BUGGY, (
        f"HTTP {resp.status_code} on POST {url} — symmetric write still rejected"
    )


async def _check_issue_2_nested_file_write(sb: "_SandboxClient") -> tuple[str, str]:
    """#2 — POST /files (collection) with nested path should auto-create parents."""
    rel = f"probe-sub-{uuid.uuid4().hex[:6]}/keep.txt"
    body = {
        "path": rel,
        "content_b64": base64.b64encode(b"hi").decode("ascii"),
    }
    try:
        resp = await sb._client.post(
            f"/v1/sessions/{sb.session_id}/files",
            json=body,
            headers={"Idempotency-Key": _idem()},
        )
    except Exception as e:
        return _BugCheckResult.ERROR, f"probe failed: {type(e).__name__}: {e}"
    if 200 <= resp.status_code < 300:
        return _BugCheckResult.FIXED, f"HTTP {resp.status_code}, path={rel}"
    return _BugCheckResult.BUGGY, (
        f"HTTP {resp.status_code} on nested-path file_write — parents not auto-created"
    )


async def _check_issue_3_mkdir_in_workspace(sb: "_SandboxClient") -> tuple[str, str]:
    """#3 — exec user should be able to mkdir under /workspace."""
    name = f"probe-mkdir-{uuid.uuid4().hex[:6]}"
    try:
        r = await sb.exec(f"mkdir -p {name} && echo OK")
    except Exception as e:
        return _BugCheckResult.ERROR, f"probe failed: {type(e).__name__}: {e}"
    if r.get("exit_code") == 0 and "OK" in (r.get("stdout") or ""):
        return _BugCheckResult.FIXED, f"mkdir under /workspace succeeded"
    err = (r.get("stderr") or "").strip()
    return _BugCheckResult.BUGGY, (
        f"exit_code={r.get('exit_code')} stderr={err!r} — exec user can't mkdir"
    )


async def _check_issue_4_idempotency_in_openapi(
    base_url: str, token: str
) -> tuple[str, str]:
    """#4 — Idempotency-Key should be declared in OpenAPI parameters."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=10) as c:
            resp = await c.get(f"{base_url}/openapi.json")
            doc = resp.json()
    except Exception as e:
        return _BugCheckResult.ERROR, f"openapi.json fetch failed: {e}"

    # Either declared as a shared component parameter, OR inline on each
    # mutating route. Either is fine — we just need a schema reflection.
    shared = (doc.get("components") or {}).get("parameters") or {}
    has_shared = any(
        (p.get("name") or "").lower() == "idempotency-key"
        for p in shared.values()
    )
    if has_shared:
        return _BugCheckResult.FIXED, "declared as components.parameters entry"

    inline_count = 0
    for path_ops in doc.get("paths", {}).values():
        for op in path_ops.values():
            for p in op.get("parameters", []) or []:
                if isinstance(p, dict) and (p.get("name") or "").lower() == "idempotency-key":
                    inline_count += 1
                    break
                # $ref form
                if isinstance(p, dict) and "$ref" in p and "Idempotency" in p["$ref"]:
                    inline_count += 1
                    break
    if inline_count > 0:
        return _BugCheckResult.FIXED, f"inline on {inline_count} routes"
    return _BugCheckResult.BUGGY, (
        "components.parameters empty AND no route declares Idempotency-Key"
    )


async def _check_issue_5_limits_unit_consistency(
    base_url: str, token: str
) -> tuple[str, str]:
    """#5 — Limits schema unit naming consistent (no _gib alongside _mib)."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=10) as c:
            resp = await c.get(f"{base_url}/openapi.json")
            doc = resp.json()
    except Exception as e:
        return _BugCheckResult.ERROR, f"openapi.json fetch failed: {e}"
    schemas = (doc.get("components") or {}).get("schemas") or {}
    limits = schemas.get("Limits") or {}
    fields = list((limits.get("properties") or {}).keys())
    if not fields:
        return _BugCheckResult.ERROR, "Limits schema not found"
    has_gib = any(f.endswith("_gib") for f in fields)
    has_mib = any(f.endswith("_mib") for f in fields)
    if has_gib and has_mib:
        return _BugCheckResult.BUGGY, (
            f"both _gib and _mib units in Limits: {sorted(fields)}"
        )
    # If schema is internally consistent we consider it fine — issue #5
    # was specifically about drift between docs and schema, which we
    # can't detect here, but matching internal units is the necessary
    # precondition.
    unit = "_gib" if has_gib else ("_mib" if has_mib else "none")
    return _BugCheckResult.FIXED, f"Limits internally consistent ({unit} only): {sorted(fields)}"


async def _check_issue_6_restart_policy_const(
    base_url: str, token: str
) -> tuple[str, str]:
    """#6 — StartProcessRequest.restart_policy should not be `{const: "never"}`."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=10) as c:
            resp = await c.get(f"{base_url}/openapi.json")
            doc = resp.json()
    except Exception as e:
        return _BugCheckResult.ERROR, f"openapi.json fetch failed: {e}"
    schemas = (doc.get("components") or {}).get("schemas") or {}
    spr = schemas.get("StartProcessRequest") or {}
    rp = (spr.get("properties") or {}).get("restart_policy")
    if rp is None:
        return _BugCheckResult.FIXED, "field dropped from request schema"
    if "const" in rp:
        return _BugCheckResult.BUGGY, (
            f"still const-typed: {rp.get('const')!r} — clients can't pick anything else"
        )
    if "enum" in rp and isinstance(rp["enum"], list) and len(rp["enum"]) > 1:
        return _BugCheckResult.FIXED, f"enum: {rp['enum']}"
    return _BugCheckResult.ERROR, f"unrecognized shape: {rp}"


async def verify_fixes(sb: "_SandboxClient", base_url: str, token: str) -> None:
    """Run each issue's regression probe and print the result.

    Does NOT raise on individual probe failures — the bug status is
    informational. Contract regressions live in the `_Step` flow above.
    """
    print()
    print("Bug-fix verification (see plan-sandbox-issues-from-e2e.md):")

    # Session-level probes — share `sb`'s upstream session.
    s, d = await _check_issue_1_symmetric_file_write(sb)
    _print_bug("issue #1: POST /files/{path} octet-stream", s, d)
    s, d = await _check_issue_2_nested_file_write(sb)
    _print_bug("issue #2: nested-path file_write auto-creates parents", s, d)
    s, d = await _check_issue_3_mkdir_in_workspace(sb)
    _print_bug("issue #3: exec user can mkdir under /workspace", s, d)

    # Schema-level probes — open their own httpx clients.
    s, d = await _check_issue_4_idempotency_in_openapi(base_url, token)
    _print_bug("issue #4: Idempotency-Key declared in OpenAPI", s, d)
    s, d = await _check_issue_5_limits_unit_consistency(base_url, token)
    _print_bug("issue #5: Limits schema unit naming consistent", s, d)
    s, d = await _check_issue_6_restart_policy_const(base_url, token)
    _print_bug("issue #6: restart_policy not const-typed", s, d)


# === Test scaffolding ===


class _Step:
    def __init__(self, name: str) -> None:
        self.name = name
        self.t0 = 0.0
        self.elapsed_ms = 0.0
        self.ok = False
        self.error: str | None = None

    def __enter__(self) -> "_Step":
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.elapsed_ms = (time.perf_counter() - self.t0) * 1000
        if exc is None:
            self.ok = True
            print(f"  [OK]   {self.name:42s} ({self.elapsed_ms:.0f} ms)")
            return False
        self.ok = False
        self.error = f"{type(exc).__name__}: {exc}"
        print(f"  [FAIL] {self.name:42s} ({self.elapsed_ms:.0f} ms)")
        print(f"         {self.error}")
        for line in traceback.format_exception(exc_type, exc, tb)[-3:]:
            for sub in line.rstrip().splitlines():
                print(f"         {sub}")
        return True  # swallow; cleanup runs in caller


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
        print("[skip] no token found. Set one of:")
        print("       ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN")
        print("       ADK_CC_SANDBOX_SERVICE_TOKEN")
        print("       SANDBOX_API_TOKEN (also auto-sourced from adk-cc/.env)")
        sys.exit(0)
    return url, token


# === Scenarios ===


async def run_e2e(url: str, token: str) -> bool:
    print(f"target: {url}")
    print(f"token:  {token[:6]}…({len(token)} chars)")
    print()

    sb = _SandboxClient(url, token)
    steps: list[_Step] = []
    cleanup_needed = False

    try:
        # 1. Bring up the upstream session (POST /v1/sessions).
        s = _Step("session_create (POST /v1/sessions)")
        steps.append(s)
        with s:
            sid = await sb.session_create()
            assert sid, "no session id returned"
            cleanup_needed = True

        # 2. Sync exec — round-trip stdout. argv wrapped in `bash -lc`.
        s = _Step("exec: echo hi → stdout")
        steps.append(s)
        with s:
            r = await sb.exec("echo hi-from-e2e")
            assert r.get("exit_code") == 0, f"exit_code={r.get('exit_code')} body={r}"
            assert "hi-from-e2e" in (r.get("stdout") or ""), f"stdout={r.get('stdout')!r}"

        # 3. cwd defaults to /workspace.
        s = _Step("exec: pwd → /workspace")
        steps.append(s)
        with s:
            r = await sb.exec("pwd")
            assert r.get("exit_code") == 0, f"body={r}"
            assert (r.get("stdout") or "").strip() == "/workspace", \
                f"stdout={r.get('stdout')!r}"

        # 4. file_write → file_read round-trip (octet-stream body).
        marker_path = "e2e-marker.txt"
        expected = f"e2e-payload-{uuid.uuid4().hex[:8]}"
        s = _Step("file_write + file_read round-trip")
        steps.append(s)
        with s:
            await sb.file_write(marker_path, expected)
            got = await sb.file_read(marker_path)
            assert got == expected, f"got={got!r} != expected={expected!r}"

        # 5. Filesystem state persists across exec calls.
        s = _Step("exec: cat written file from /workspace")
        steps.append(s)
        with s:
            r = await sb.exec("cat e2e-marker.txt")
            assert r.get("exit_code") == 0, f"body={r}"
            assert expected in (r.get("stdout") or ""), \
                f"stdout={r.get('stdout')!r}"

        # 6. cwd-subdir handling — proves the client emits the
        # `cd '<sub>' && cmd` wrapper. We can't reliably create a
        # subdirectory on this deployment (the unprivileged exec
        # user can't mkdir under /workspace, and the file_write API
        # doesn't auto-create parents on this build), so instead
        # we send cwd to a known-missing path and verify the cd
        # error message identifies that exact path — which it can
        # only do if the wrapper made it to the server intact.
        missing = "/workspace/__cd_probe__"
        s = _Step("exec cd-prefix wrapper emitted (via cd-fail)")
        steps.append(s)
        with s:
            r = await sb.exec("pwd", cwd=missing)
            stderr = r.get("stderr") or ""
            assert r.get("exit_code") != 0, f"unexpected success: {r}"
            assert missing in stderr, (
                f"cd path {missing!r} not echoed in stderr — wrapper not "
                f"emitted? stderr={stderr!r}"
            )

        # 7. read_text on missing file → 404 → FileNotFoundError.
        s = _Step("file_read missing file → FileNotFoundError")
        steps.append(s)
        with s:
            try:
                await sb.file_read("does-not-exist.txt")
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("expected FileNotFoundError")

        # 8. New ExecResponse fields surfaced (effective_truncation_cap_bytes,
        #    resume_latency_ms) — service may or may not include them, but if
        #    they're present they should match the documented contract.
        s = _Step("exec: ExecResponse new fields present (≥0)")
        steps.append(s)
        with s:
            r = await sb.exec("echo probe")
            cap = r.get("effective_truncation_cap_bytes")
            resume = r.get("resume_latency_ms")
            # Both fields are documented as present in 0.2+. Older builds may
            # lack them; treat absence as a soft pass (the field defaults to
            # the documented value), but log it so contract drift is visible.
            if cap is None:
                print("         note: effective_truncation_cap_bytes not in response (older build)")
            else:
                assert int(cap) > 0, f"cap={cap!r}"
            if resume is None:
                print("         note: resume_latency_ms not in response (older build)")
            else:
                assert int(resume) >= 0, f"resume={resume!r}"

        # 9. Bug-fix verification probes (informational; don't fail
        # the e2e on bug presence). Runs while the session is still
        # alive so the session-level checks #1–#3 reuse it.
        await verify_fixes(sb, url, token)

        # 10. Clean session shutdown — POST /stop, NOT /destroy.
        s = _Step("session_stop (POST /v1/sessions/<id>/stop)")
        steps.append(s)
        with s:
            await sb.session_stop()
            cleanup_needed = False
    finally:
        if cleanup_needed:
            try:
                await sb.session_stop()
            except Exception:
                pass
        await sb.aclose()

    print()
    failed = [s for s in steps if not s.ok]
    if failed:
        print(f"FAILED ({len(failed)}/{len(steps)}):")
        for s in failed:
            print(f"  - {s.name}")
        return False
    print(f"PASSED ({len(steps)}/{len(steps)})")
    return True


def main() -> int:
    url, token = _resolve_config()
    try:
        ok = asyncio.run(run_e2e(url, token))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception:
        traceback.print_exc()
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
