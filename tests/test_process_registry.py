"""Background-process registry (#108): ownership, logs, ports, termination.

The feature's whole risk is lifecycle — a backgrounded process is outside the
turn's lifetime, so it escapes every cleanup we have, and its own process
group means it survives the backend dying too (#98's orphan class, one level
down). These tests pin the parts that make that safe: pgid recorded, group
terminate, boot sweep reconciling the index against reality, and logs that
outlive the process.

Uses REAL short-lived processes (`sleep`, a python one-liner) rather than
mocks: the thing under test is process lifecycle, and a mock proves nothing
about killpg.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_process_registry.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
os.environ.setdefault("ADK_CC_DESKTOP", "1")
_TMP = tempfile.mkdtemp(prefix="procreg-")
os.environ.setdefault("ADK_CC_DESKTOP_DATA", _TMP)

from adk_cc.sandbox import process_registry as PR  # noqa: E402
from adk_cc.sandbox.backends.noop_backend import NoopBackend  # noqa: E402
from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig  # noqa: E402

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _reg(sub: str) -> PR.ProcessRegistry:
    return PR.ProcessRegistry(Path(_TMP) / sub)


async def _start(backend, cmd: str, **kw):
    return await backend.start_background(
        cmd, fs_write=FsWriteConfig(allow_paths=("/tmp/**",)),
        network=NetworkConfig(), cwd="/tmp",
        session_key=kw.pop("session_key", "a/u/s"),
        project_id=kw.pop("project_id", "proj"), **kw)


def test_redaction_and_labels() -> None:
    reg = _reg("redact")
    rec = reg.create(session_key="a/u/s", project_id="p", label="",
                     command="GITHUB_TOKEN=ghp_verysecret npm run deploy",
                     cwd="/tmp", backend="noop")
    check("secrets are redacted before the command is stored",
          "ghp_verysecret" not in rec.command and "***" in rec.command,
          rec.command)
    check("a label is derived when none is given", bool(rec.label), rec.label)
    # Over-redaction is its own bug: the first live run recorded
    # `export PATH=***` because "PATH" contains "PAT", making the displayed
    # command useless — the very thing showing it is for.
    keep = reg.create(
        session_key="a/u/s", project_id="p", label="",
        command='export PATH="$PWD/.venv/bin:$PATH"; python3 -m http.server 8899',
        cwd="/tmp", backend="noop")
    check("PATH is NOT mistaken for a secret",
          "PATH=" in keep.command and "***" not in keep.command, keep.command)
    for cmd, red in (("API_KEY=abc run", True), ("MY_PAT=xyz go", True),
                     ("DB_PASSWORD=hunter2 psql", True),
                     ("PATHOLOGY=1 test", False)):
        r = reg.create(session_key="s", project_id="p", label="", command=cmd,
                       cwd="/tmp", backend="noop")
        check(f"redaction of {cmd!r} -> {red}", ("***" in r.command) == red,
              r.command)


def test_port_from_command_and_log() -> None:
    reg = _reg("ports")
    for cmd, want in (("python3 -m http.server 8000", 8000),
                      ("npm run dev -- --port 5173", 5173),
                      ("uvicorn app:main --port=9000", 9000),
                      ("echo hello", None)):
        rec = reg.create(session_key="s", project_id="p", label="", command=cmd,
                         cwd="/tmp", backend="noop")
        check(f"port from {cmd!r} -> {want}", rec.port == want, rec.port)
    # …and from the log when the command does not say (the common Vite case).
    rec = reg.create(session_key="s", project_id="p", label="", command="npm run dev",
                     cwd="/tmp", backend="noop")
    reg.append_log(rec.id, b"  VITE ready\n  Local: http://localhost:5174/\n")
    check("port sniffed from output when the command is silent",
          reg.get(rec.id).port == 5174, reg.get(rec.id).port)


def test_log_survives_and_tails() -> None:
    reg = _reg("logs")
    rec = reg.create(session_key="s", project_id="p", label="l",
                     command="x", cwd="/tmp", backend="noop")
    reg.append_log(rec.id, b"line one\n")
    reg.append_log(rec.id, b"line two\n")
    check("log is readable", "line two" in reg.read_log(rec.id))
    # A FILE, not a memory buffer: a fresh registry over the same dir sees it.
    reg2 = PR.ProcessRegistry(Path(_TMP) / "logs")
    check("log and record survive a registry restart",
          "line one" in reg2.read_log(rec.id) and reg2.get(rec.id) is not None)


def test_real_process_lifecycle() -> None:
    async def main():
        reg_dir = Path(_TMP) / "live"
        PR._reset_for_test(reg_dir)
        backend = NoopBackend()
        rec = await _start(backend, "sleep 30", label="sleeper")
        reg = PR.get_registry()
        pid, pgid = rec["pid"], rec["pgid"]
        check("a real process starts and is recorded running",
              rec["status"] == "running" and pid and pgid == pid, rec)
        check("the process is genuinely alive", PR._pgid_alive(pgid))

        ok = reg.terminate(rec["id"])
        check("terminate reports success", ok)
        await asyncio.sleep(0.3)
        check("the process GROUP is gone", not PR._pgid_alive(pgid))
        check("status records a user kill, distinct from a crash",
              reg.get(rec["id"]).status == "killed",
              reg.get(rec["id"]).status)

        # A natural exit is recorded with its code (and NOT as "killed").
        rec2 = await _start(backend, "sh -c 'echo bye; exit 3'", label="quick")
        for _ in range(40):
            await asyncio.sleep(0.1)
            if reg.get(rec2["id"]).status != "running":
                break
        r2 = reg.get(rec2["id"])
        check("a natural non-zero exit is 'failed' with its code",
              r2.status == "failed" and r2.exit_code == 3,
              (r2.status, r2.exit_code))
        check("its output was captured to the log",
              "bye" in reg.read_log(rec2["id"]))

    asyncio.run(main())


def test_kills_the_whole_group_not_just_the_shell() -> None:
    """The reason terminate targets the GROUP: a server that forks (or a
    shell that backgrounds a child) leaves the real process holding the port
    when only the shell is killed."""
    async def main():
        PR._reset_for_test(Path(_TMP) / "group")
        backend = NoopBackend()
        marker = Path(_TMP) / "group-child-alive"
        rec = await _start(
            backend,
            f"sh -c 'sleep 30 & echo $! > {marker}; wait'",
            label="forker")
        reg = PR.get_registry()
        for _ in range(30):
            await asyncio.sleep(0.1)
            if marker.exists() and marker.read_text().strip():
                break
        child_pid = int(marker.read_text().strip())
        check("the forked grandchild is running",
              _pid_alive(child_pid), child_pid)
        reg.terminate(rec["id"])
        await asyncio.sleep(0.4)
        check("terminating the group kills the grandchild too",
              not _pid_alive(child_pid), child_pid)

    asyncio.run(main())


def test_boot_sweep_reconciles_the_index() -> None:
    """#98's lesson one level down: a process recorded as running that is
    still alive belongs to a previous backend generation and must be
    REPORTED (adopted), not silently forgotten; one that is gone must be
    marked, not left claiming to run forever."""
    d = Path(_TMP) / "sweep"
    reg = PR.ProcessRegistry(d)
    alive = subprocess.Popen(["sleep", "30"], start_new_session=True)
    live = reg.create(session_key="s", project_id="p", label="live",
                      command="sleep 30", cwd="/tmp", backend="noop")
    reg.mark_started(live.id, pid=alive.pid, pgid=alive.pid)
    dead = reg.create(session_key="s", project_id="p", label="dead",
                      command="sleep 0", cwd="/tmp", backend="noop")
    reg.mark_started(dead.id, pid=999999, pgid=999999)   # never existed

    fresh = PR.ProcessRegistry(d)          # simulates a backend restart
    stats = fresh.sweep()
    check("a still-running process is adopted, not forgotten",
          fresh.get(live.id).status == "running" and fresh.get(live.id).adopted,
          fresh.get(live.id))
    check("a vanished process stops claiming to run",
          fresh.get(dead.id).status == "unknown", fresh.get(dead.id))
    check("the sweep reports what it found", stats == {"alive": 1, "gone": 1},
          stats)
    alive.kill()


def test_listing_is_project_scoped_and_ordered() -> None:
    reg = _reg("listing")
    a = reg.create(session_key="s1", project_id="pA", label="a", command="x",
                   cwd="/tmp", backend="noop")
    reg.mark_started(a.id, pid=1, pgid=1)
    b = reg.create(session_key="s2", project_id="pA", label="b", command="y",
                   cwd="/tmp", backend="noop")
    reg.mark_exited(b.id, exit_code=0)
    reg.create(session_key="s3", project_id="pB", label="c", command="z",
               cwd="/tmp", backend="noop")
    rows = reg.list(project_id="pA")
    check("only this project's processes are listed",
          [r.label for r in rows] == ["a", "b"], [r.label for r in rows])
    check("a process started in ANOTHER session of the project is still shown",
          any(r.session_key == "s2" for r in rows))
    check("running sorts before finished", rows[0].status == "running")
    check("a finished record can be forgotten", reg.forget(b.id))
    check("a running one cannot", not reg.forget(a.id))


def test_no_backend_claims_background_it_cannot_do() -> None:
    """The inheritance hazard: LocalContainerBackend extends NoopBackend, so
    flipping `supports_background = True` on the parent silently gave every
    container backend the HOST spawner — a background process escaping the
    container entirely, which is the one thing that backend exists to prevent.

    A backend may only advertise background support if it overrides the
    spawner itself."""
    from adk_cc.sandbox.backends.base import SandboxBackend
    from adk_cc.sandbox.backends.local_container_backend import (
        LocalContainerBackend, UnavailableSandboxBackend)
    from adk_cc.sandbox.backends.ssh_backend import SshBackend

    for cls in (LocalContainerBackend, UnavailableSandboxBackend):
        check(f"{cls.__name__} does NOT claim background support",
              cls.supports_background is False)
        check(f"{cls.__name__} refuses to spawn even if called",
              cls.start_background is not NoopBackend.start_background)
    for cls in (NoopBackend, SshBackend):
        check(f"{cls.__name__} advertises background AND implements it",
              cls.supports_background
              and cls.start_background is not SandboxBackend.start_background)
    for cls in SandboxBackend.__subclasses__():
        if cls.supports_background:
            check(f"{cls.__name__} implements its own start_background",
                  cls.start_background is not SandboxBackend.start_background,
                  "advertises background but inherits the raising default")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    test_redaction_and_labels()
    test_port_from_command_and_log()
    test_log_survives_and_tails()
    test_real_process_lifecycle()
    test_kills_the_whole_group_not_just_the_shell()
    test_boot_sweep_reconciles_the_index()
    test_listing_is_project_scoped_and_ordered()
    test_no_backend_claims_background_it_cannot_do()
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
