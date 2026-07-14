"""EVIDENCE for review findings #2, #3, #5, and the create-failure path (#7).

Each check drives the REAL backend (against Docker/Podman where needed) and
prints CONFIRMED / REFUTED with the observed values, so the review rests on
reproductions, not assertions. SKIPS the container checks when no runtime.

Run: PYTHONPATH=agents .venv/bin/python tests/e2e_container_sandbox_review.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.sandbox.backends import container_runtime as cr
from adk_cc.sandbox.backends.container_runtime import detect_runtime
from adk_cc.sandbox.backends.local_container_backend import LocalContainerBackend, sweep_orphans
from adk_cc.sandbox.config import FsReadConfig, FsWriteConfig, NetworkConfig
from adk_cc.sandbox.workspace import WorkspaceRoot

_confirmed: list[str] = []


def verdict(finding: str, confirmed: bool, observed: str) -> None:
    tag = "CONFIRMED" if confirmed else "REFUTED"
    print(f"  [{tag}] {finding}\n            observed: {observed}")
    if confirmed:
        _confirmed.append(finding)


def _ws(path: str, extra=()) -> WorkspaceRoot:
    return WorkspaceRoot(tenant_id="local", session_id="rev", abs_path=path, extra_roots=tuple(extra))


async def _exec(b, cmd, cwd, timeout=30):
    return await b.exec(cmd, fs_write=FsWriteConfig(), network=NetworkConfig(), timeout_s=timeout, cwd=cwd)


# ---- #2: silent fail-open to host exec -------------------------------------

def check_fail_open_is_silent() -> None:
    from adk_cc import deployment
    from adk_cc.sandbox import make_default_backend
    from adk_cc.sandbox.backends.noop_backend import NoopBackend

    saved = {k: os.environ.get(k) for k in ("ADK_CC_DESKTOP", "ADK_CC_SANDBOX_MODE", "ADK_CC_SANDBOX_BACKEND")}
    orig_detect = cr.detect_runtime
    # capture WARNING+ from the whole adk_cc.sandbox tree
    records: list[logging.LogRecord] = []
    h = logging.Handler()
    h.emit = lambda r: records.append(r)  # type: ignore[assignment]
    log = logging.getLogger("adk_cc.sandbox")
    log.addHandler(h)
    log.setLevel(logging.WARNING)
    try:
        os.environ["ADK_CC_DESKTOP"] = "1"
        os.environ["ADK_CC_SANDBOX_MODE"] = "container"
        os.environ.pop("ADK_CC_SANDBOX_BACKEND", None)
        cr.detect_runtime = lambda: None  # simulate: runtime missing / cache pinned None

        selected = deployment.sandbox_backend_name()
        backend = make_default_backend(session_id="failopen")
        warned = any(r.levelno >= logging.WARNING for r in records)
        # The BUG is a SILENT fallback to host. After the fix, opting in still
        # falls back to host (usable default) but LOUDLY — a warning is logged.
        # So "confirmed" = fell back to noop AND stayed silent.
        confirmed = isinstance(backend, NoopBackend) and not warned
        verdict(
            "#2 sandbox falls open to HOST exec SILENTLY when the runtime is absent",
            confirmed,
            f"mode=container → selected={selected!r}, backend={type(backend).__name__}, "
            f"warning_logged={warned}",
        )
    finally:
        cr.detect_runtime = orig_detect
        log.removeHandler(h)
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


# ---- #3: exit 137 (SIGKILL/OOM) mislabeled as timeout, stderr clobbered -----

async def check_137_conflates_timeout(rt) -> None:
    b = LocalContainerBackend(session_id="rev-137", runtime=rt, network_enabled=False)
    proj = tempfile.mkdtemp(prefix="rev-137-")
    try:
        await b.ensure_workspace(_ws(proj))
        # The command self-kills with SIGKILL (exit 137) WELL WITHIN the 30s
        # timeout — i.e. NOT a timeout. A real OOM-kill under --memory would look
        # identical to the backend.
        r = await _exec(b, "echo real-error >&2; kill -9 $$", cwd=proj, timeout=30)
        confirmed = r.timed_out is True and "timed out" in r.stderr and "real-error" not in r.stderr
        verdict(
            "#3 exit 137 (SIGKILL/OOM, not a timeout) is reported as timed_out + stderr clobbered",
            confirmed,
            f"exit={r.exit_code}, timed_out={r.timed_out}, stderr={r.stderr.strip()!r}",
        )
    finally:
        await b.close()


# ---- #5: a dir granted mid-session is invisible to the container ------------

async def check_midsession_grant_invisible(rt) -> None:
    b = LocalContainerBackend(session_id="rev-grant", runtime=rt, network_enabled=False)
    proj = tempfile.mkdtemp(prefix="rev-grant-proj-")
    granted = tempfile.mkdtemp(prefix="rev-grant-extra-")
    Path(granted, "granted_file.txt").write_text("granted-data")
    try:
        # session starts with only the project mounted; container is created here
        await b.ensure_workspace(_ws(proj))
        await _exec(b, "true", cwd=proj)  # forces container creation

        # ... user grants `granted` mid-session → get_workspace folds it into a
        # fresh WorkspaceRoot.extra_roots, and ensure_workspace is called again.
        await b.ensure_workspace(_ws(proj, extra=(granted,)))

        # host-direct file tool CAN read it (inherited from NoopBackend)
        text = await b.read_text(str(Path(granted, "granted_file.txt")),
                                 fs_read=FsReadConfig(allow_paths=(f"{granted}/**",)))
        file_tool_ok = text.strip() == "granted-data"

        # but run_bash (containerized) CANNOT — the mount was fixed at create
        r = await _exec(b, f"cat {os.path.realpath(granted)}/granted_file.txt 2>&1 || echo NOT-IN-CONTAINER",
                        cwd=proj)
        shell_blind = "granted-data" not in r.stdout

        confirmed = file_tool_ok and shell_blind
        verdict(
            "#5 a dir granted mid-session is visible to file tools but NOT to run_bash",
            confirmed,
            f"file_tool_read_ok={file_tool_ok}, shell_sees_it={not shell_blind} "
            f"(shell said {r.stdout.strip()!r})",
        )
    finally:
        await b.close()


# ---- #7/create-failure: a bad/absent image raises out of the tool ----------

async def check_create_failure_raises(rt) -> None:
    b = LocalContainerBackend(session_id="rev-badimg", runtime=rt,
                              image="adk-cc-nonexistent-zzz:review", network_enabled=False)
    proj = tempfile.mkdtemp(prefix="rev-badimg-")
    await b.ensure_workspace(_ws(proj))
    raised = None
    result = None
    try:
        result = await _exec(b, "echo hi", cwd=proj, timeout=10)
    except Exception as e:  # noqa: BLE001
        raised = e
    finally:
        await b.close()
    # a clean design returns an ExecResult(exit_code!=0); this path raises instead
    confirmed = raised is not None and result is None
    verdict(
        "#7 a create failure (absent image) raises a raw exception out of run_bash "
        "instead of a clean ExecResult",
        confirmed,
        (f"raised {type(raised).__name__}: {str(raised)[:90]!r}" if raised
         else f"returned {result}"),
    )


def main() -> int:
    print("container sandbox — review evidence:")
    check_fail_open_is_silent()  # no runtime needed

    rt = detect_runtime()
    if rt is None:
        print("  [SKIP] no container runtime — skipping #3/#5/#7 (need a live container)")
    else:
        try:
            asyncio.run(check_137_conflates_timeout(rt))
            asyncio.run(check_midsession_grant_invisible(rt))
            asyncio.run(check_create_failure_raises(rt))
        finally:
            sweep_orphans(rt)

    if _confirmed:
        print(f"\nFAIL: {len(_confirmed)} finding(s) REGRESSED: {_confirmed}")
        return 1
    print("\nPASS: no findings reproduced (all fixed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
