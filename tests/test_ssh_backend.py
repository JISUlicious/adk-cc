"""Unit tests for `SshBackend` — contract behavior over a FAKE transport.

The transport itself is proven live in e2e_ssh_transport.py; here we pin
the backend's contract: allow-path fail-fast BEFORE any transport call,
runtime-env merging into exec, error mapping (transport failure → exec
ExecResult(-1) / file-op SandboxCapacityError), ensure_workspace bring-up,
the remote-flagged WorkspaceRoot, and factory env dispatch.

Run: `uv run python tests/test_ssh_backend.py`
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.sandbox.backends.ssh_backend import SshBackend  # noqa: E402
from adk_cc.sandbox.config import (  # noqa: E402
    ExecResult,
    FsReadConfig,
    FsWriteConfig,
    NetworkConfig,
    SandboxCapacityError,
    SandboxViolation,
)
from adk_cc.sandbox.ssh_transport import SshConnectionError  # noqa: E402
from adk_cc.sandbox.workspace import WorkspaceRoot  # noqa: E402

_WS = "/home/dev/proj"


class FakeTransport:
    """Records calls; scripted responses. Raises when told to."""

    def __init__(self) -> None:
        self.host = "dev@fake"
        self.calls: list[tuple] = []
        self.fail_connect = False
        self.files: dict[str, bytes] = {}

    async def run(self, cmd, *, env=None, cwd=None, timeout_s=60.0):
        self.calls.append(("run", cmd, dict(env or {}), cwd, timeout_s))
        if self.fail_connect:
            raise SshConnectionError("ssh to 'dev@fake' failed: refused")
        for needle, res in getattr(self, "scripted", []):
            if needle in cmd:
                return res
        return ExecResult(exit_code=0, stdout=f"ran:{cmd}", stderr="")

    async def run_stream(self, cmd, *, env=None, cwd=None, timeout_s=60.0):
        self.calls.append(("run_stream", cmd, dict(env or {}), cwd, timeout_s))
        if self.fail_connect:
            raise SshConnectionError("ssh to 'dev@fake' failed: refused")
        from adk_cc.sandbox.config import ExecChunk

        yield ExecChunk(kind="stdout", data="live")
        yield ExecChunk(
            kind="result", result=ExecResult(exit_code=0, stdout="live", stderr="")
        )

    async def read_file(self, path, *, timeout_s=60.0):
        self.calls.append(("read_file", path))
        if self.fail_connect:
            raise SshConnectionError("refused: Connection refused")
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path, data, *, mkdirs=True, timeout_s=60.0):
        self.calls.append(("write_file", path))
        if self.fail_connect:
            raise SshConnectionError("refused: Connection refused")
        self.files[path] = data

    async def probe(self, *, refresh=False, timeout_s=20.0):
        self.calls.append(("probe",))
        if self.fail_connect:
            raise SshConnectionError("refused: Connection refused")
        return {"home": "/home/dev", "git": True, "uname": "Linux"}


def _backend(t=None) -> tuple[SshBackend, FakeTransport]:
    t = t or FakeTransport()
    b = SshBackend(session_id="s1", tenant_id="acme", transport=t)
    return b, t


def _ws() -> WorkspaceRoot:
    return WorkspaceRoot(
        tenant_id="acme", session_id="s1", abs_path=_WS, remote=True
    )


def _fsw(ws: WorkspaceRoot) -> FsWriteConfig:
    return ws.fs_write_config()


def _fsr(ws: WorkspaceRoot) -> FsReadConfig:
    return ws.fs_read_config()


async def test_remote_workspace_skips_local_realpath():
    """The load-bearing flag: a remote /home/... path must NOT be realpath'd
    against the local fs (macOS rewrites /home/* → /System/Volumes/Data/...)."""
    ws = _ws()
    assert ws.abs_path == _WS, ws.abs_path
    local = WorkspaceRoot(tenant_id="a", session_id="s", abs_path="/tmp")
    # Local (non-remote) roots still canonicalize (macOS: /tmp → /private/tmp).
    assert local.abs_path == os.path.realpath("/tmp"), local.abs_path
    print("OK remote_workspace_skips_local_realpath")


async def test_ensure_workspace_probes_and_mkdirs():
    b, t = _backend()
    await b.ensure_workspace(_ws())
    kinds = [c[0] for c in t.calls]
    assert "probe" in kinds, kinds
    mk = next(c for c in t.calls if c[0] == "run")
    assert "mkdir -p" in mk[1] and _WS in mk[1], mk
    print("OK ensure_workspace_probes_and_mkdirs")


async def test_ensure_workspace_unreachable_raises_capacity_error():
    b, t = _backend()
    t.fail_connect = True
    try:
        await b.ensure_workspace(_ws())
    except SandboxCapacityError as e:
        assert isinstance(e, SandboxViolation)  # retryable + legacy-catchable
        print("OK ensure_workspace_unreachable_raises_capacity_error")
        return
    raise AssertionError("expected SandboxCapacityError")


async def test_exec_merges_runtime_env_and_passes_cwd():
    b, t = _backend()
    ws = _ws()
    await b.ensure_workspace(ws)
    # Wire a static env spec through the base-class runtime env machinery.
    from adk_cc.sandbox.sandbox_env import SandboxEnvSpec

    b.configure_runtime_env(env_spec=SandboxEnvSpec(static={"TZ": "UTC"}))
    res = await b.exec(
        "echo hi", fs_write=_fsw(ws), network=NetworkConfig(), timeout_s=9, cwd=_WS
    )
    assert res.exit_code == 0
    call = [c for c in t.calls if c[0] == "run" and c[1] == "echo hi"][0]
    assert call[2] == {"TZ": "UTC"}, call  # runtime env reached the transport
    assert call[3] == _WS and call[4] == 9, call
    print("OK exec_merges_runtime_env_and_passes_cwd")


async def test_exec_cwd_outside_workspace_rejected():
    b, t = _backend()
    ws = _ws()
    try:
        await b.exec(
            "ls", fs_write=_fsw(ws), network=NetworkConfig(), timeout_s=5, cwd="/etc"
        )
    except SandboxViolation:
        assert not [c for c in t.calls if c[0] == "run"], "must fail BEFORE transport"
        print("OK exec_cwd_outside_workspace_rejected")
        return
    raise AssertionError("expected SandboxViolation for cwd outside workspace")


async def test_exec_transport_error_returns_failed_execresult():
    b, t = _backend()
    ws = _ws()
    t.fail_connect = True
    res = await b.exec(
        "echo hi", fs_write=_fsw(ws), network=NetworkConfig(), timeout_s=5, cwd=_WS
    )
    assert res.exit_code == -1 and "transport error" in res.stderr, res
    print("OK exec_transport_error_returns_failed_execresult")


async def test_exec_stream_yields_live_then_result():
    b, _t = _backend()
    ws = _ws()
    chunks = [
        c
        async for c in b.exec_stream(
            "echo s", fs_write=_fsw(ws), network=NetworkConfig(), timeout_s=5, cwd=_WS
        )
    ]
    assert [c.kind for c in chunks] == ["stdout", "result"], chunks
    assert chunks[-1].result and chunks[-1].result.exit_code == 0
    print("OK exec_stream_yields_live_then_result")


async def test_file_io_round_trip_and_allow_paths():
    b, t = _backend()
    ws = _ws()
    await b.write_text(f"{_WS}/a.txt", "héllo", fs_write=_fsw(ws))
    got = await b.read_text(f"{_WS}/a.txt", fs_read=_fsr(ws))
    assert got == "héllo", got

    # Outside the workspace → SandboxViolation BEFORE any transport call.
    n = len(t.calls)
    for fn in (
        lambda: b.read_text("/etc/passwd", fs_read=_fsr(ws)),
        lambda: b.write_text("/etc/pwned", "x", fs_write=_fsw(ws)),
    ):
        try:
            await fn()
            raise AssertionError("expected SandboxViolation")
        except SandboxViolation as e:
            assert not isinstance(e, SandboxCapacityError)
    assert len(t.calls) == n, "allow-path check must not hit the transport"
    print("OK file_io_round_trip_and_allow_paths")


async def test_file_io_transport_error_maps_to_capacity_error():
    b, t = _backend()
    ws = _ws()
    t.fail_connect = True
    try:
        await b.read_text(f"{_WS}/a.txt", fs_read=_fsr(ws))
    except SandboxCapacityError:
        print("OK file_io_transport_error_maps_to_capacity_error")
        return
    raise AssertionError("expected SandboxCapacityError")


async def test_missing_file_raises_file_not_found():
    b, _t = _backend()
    ws = _ws()
    try:
        await b.read_text(f"{_WS}/nope.txt", fs_read=_fsr(ws))
    except FileNotFoundError:
        print("OK missing_file_raises_file_not_found")
        return
    raise AssertionError("expected FileNotFoundError")


async def test_tools_resolve_is_lexical_for_remote_workspace():
    """tools/_fs.resolve() must NOT consult the local fs for a remote
    workspace: on macOS, realpath rewrites /home/* (automount) and ~
    expands to the LOCAL home — both would target the wrong machine."""
    from adk_cc.sandbox.workspace import set_workspace
    from adk_cc.tools._fs import resolve

    class _Ctx:
        def __init__(self):
            self.state: dict = {}

    ctx = _Ctx()
    set_workspace(ctx, _ws())  # remote /home/dev/proj

    # Relative anchors under the REMOTE root, verbatim (no local realpath).
    assert str(resolve("a/b.txt", ctx)) == f"{_WS}/a/b.txt"
    # Absolute remote paths pass through untouched (macOS would otherwise
    # rewrite /home/dev/... via the automount).
    assert str(resolve(f"{_WS}/x.py", ctx)) == f"{_WS}/x.py"
    # `..` collapses lexically; the escape is then for allow-paths to deny.
    assert str(resolve("../outside.txt", ctx)) == "/home/dev/outside.txt"
    # `~` is NOT expanded against the local home.
    local_home = os.path.expanduser("~")
    assert not str(resolve("~/leak.txt", ctx)).startswith(local_home)
    print("OK tools_resolve_is_lexical_for_remote_workspace")


async def test_factory_env_dispatch():
    from adk_cc.sandbox import make_default_backend

    old = {
        k: os.environ.get(k)
        for k in (
            "ADK_CC_SANDBOX_BACKEND",
            "ADK_CC_SSH_HOST",
            "ADK_CC_SSH_PORT",
            "ADK_CC_SSH_WORKSPACE_PATH",
        )
    }
    try:
        os.environ["ADK_CC_SANDBOX_BACKEND"] = "ssh"
        os.environ["ADK_CC_SSH_HOST"] = "dev@remotebox"
        os.environ["ADK_CC_SSH_PORT"] = "2201"
        os.environ["ADK_CC_SSH_WORKSPACE_PATH"] = "/home/dev/proj"
        b = make_default_backend(session_id="s1", tenant_id="t1")
        assert isinstance(b, SshBackend), type(b)
        assert b.host == "dev@remotebox"
        # default_workspace() returns the remote-flagged root, untouched.
        from adk_cc.sandbox.workspace import default_workspace

        ws = default_workspace()
        assert ws.remote and ws.abs_path == "/home/dev/proj", ws
        assert b.container_cwd(ws.abs_path) == "/home/dev/proj"  # identity
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("OK factory_env_dispatch")


# ---- remote background processes (#108) ------------------------------
# The mechanism: setsid at launch so pgid == pid, then terminate is just
# another exec (`kill -TERM -<pgid>`) over the same ControlMaster. These pin
# the parts that make that safe — the pgid is actually captured, the kill
# targets the GROUP (not the pid), and an unreachable host degrades to a
# recorded failure instead of a lie about a running server.

def _bg_registry(tag: str):
    import tempfile
    from pathlib import Path

    from adk_cc.sandbox import process_registry as PR

    PR._reset_for_test(Path(tempfile.mkdtemp(prefix=f"sshbg-{tag}-")))
    return PR.get_registry()


async def test_background_launch_captures_pgid_and_detaches():
    reg = _bg_registry("launch")
    b, t = _backend()
    t.scripted = [("setsid", ExecResult(exit_code=0, stdout="4242\n", stderr=""))]
    rec = await b.start_background(
        "npm run dev", fs_write=_fsw(_ws()),
        network=NetworkConfig(), cwd=_WS, label="dev",
        session_key="s1", project_id="p1")
    launch = next(c[1] for c in t.calls if c[0] == "run" and "setsid" in c[1])
    assert "setsid" in launch, launch
    assert "nohup" in launch and "< /dev/null" in launch, launch
    assert "&" in launch and "echo $!" in launch, launch
    assert rec["status"] == "running", rec
    assert reg.get(rec["id"]).pid == 4242
    # pgid == pid is the whole point: it is what makes the group kill reach
    # anything the server forks.
    assert reg.get(rec["id"]).pgid == 4242
    assert reg.get(rec["id"]).can_terminate is True
    print("OK background_launch_captures_pgid_and_detaches")


async def test_background_launch_failure_is_recorded_not_claimed_running():
    reg = _bg_registry("fail")
    b, t = _backend()
    t.scripted = [("setsid", ExecResult(exit_code=127, stdout="",
                                        stderr="sh: npm: not found"))]
    rec = await b.start_background(
        "npm run dev", fs_write=_fsw(_ws()),
        network=NetworkConfig(), cwd=_WS, session_key="s1", project_id="p1")
    assert rec["status"] == "failed", rec
    assert "not found" in reg.read_log(rec["id"]), reg.read_log(rec["id"])
    print("OK background_launch_failure_is_recorded_not_claimed_running")


async def test_background_launch_when_host_unreachable():
    reg = _bg_registry("unreach")
    b, t = _backend()
    t.fail_connect = True
    rec = await b.start_background(
        "npm run dev", fs_write=_fsw(_ws()),
        network=NetworkConfig(), cwd=_WS, session_key="s1", project_id="p1")
    assert rec["status"] == "failed", rec
    assert "transport error" in reg.read_log(rec["id"])
    print("OK background_launch_when_host_unreachable")


async def test_background_cwd_outside_workspace_never_reaches_the_host():
    b, t = _backend()
    try:
        await b.start_background(
            "rm -rf /", fs_write=_fsw(_ws()),
            network=NetworkConfig(), cwd="/etc", session_key="s", project_id="p")
        raise AssertionError("expected SandboxViolation")
    except SandboxViolation:
        pass
    assert not t.calls, t.calls
    print("OK background_cwd_outside_workspace_never_reaches_the_host")


async def test_terminate_kills_the_remote_GROUP():
    reg = _bg_registry("term")
    b, t = _backend()
    t.scripted = [("setsid", ExecResult(exit_code=0, stdout="777\n", stderr=""))]
    rec = await b.start_background(
        "python3 -m http.server 8000",
        fs_write=_fsw(_ws()),
        network=NetworkConfig(), cwd=_WS, session_key="s1", project_id="p1")
    t.calls.clear()
    ok = await b.terminate_background(rec["id"])
    assert ok
    kill = next(c[1] for c in t.calls if c[0] == "run" and "kill" in c[1])
    # The MINUS is the bug this pins: `kill -TERM 777` leaves a forking
    # server's children holding the port.
    assert "kill -TERM -777" in kill, kill
    assert "kill -KILL -777" in kill, kill
    assert reg.get(rec["id"]).status == "killed"
    print("OK terminate_kills_the_remote_GROUP")


async def test_terminate_reports_failure_when_host_is_gone():
    _bg_registry("termfail")
    b, t = _backend()
    t.scripted = [("setsid", ExecResult(exit_code=0, stdout="888\n", stderr=""))]
    rec = await b.start_background(
        "sleep 999", fs_write=_fsw(_ws()),
        network=NetworkConfig(), cwd=_WS, session_key="s1", project_id="p1")
    t.fail_connect = True
    # A kill that never left the machine must NOT be reported as a stop —
    # the UI would show "stopped" while the server keeps serving.
    assert await b.terminate_background(rec["id"]) is False
    print("OK terminate_reports_failure_when_host_is_gone")


async def test_local_registry_refuses_to_signal_a_remote_pgid():
    """A remote pgid is a number on ANOTHER machine; signalling it here would
    hit an unrelated local process (or nothing). The registry must decline."""
    reg = _bg_registry("localsig")
    b, t = _backend()
    t.scripted = [("setsid", ExecResult(exit_code=0, stdout="999\n", stderr=""))]
    rec = await b.start_background(
        "sleep 999", fs_write=_fsw(_ws()),
        network=NetworkConfig(), cwd=_WS, session_key="s1", project_id="p1")
    assert reg.terminate(rec["id"]) is False
    assert reg.get(rec["id"]).status == "running"
    print("OK local_registry_refuses_to_signal_a_remote_pgid")


async def test_log_sync_pulls_the_remote_tail_and_sniffs_the_port():
    reg = _bg_registry("logsync")
    b, t = _backend()
    t.scripted = [
        ("setsid", ExecResult(exit_code=0, stdout="1234\n", stderr="")),
        ("tail -c", ExecResult(exit_code=0,
                               stdout="VITE ready\nLocal: http://localhost:5173/\n",
                               stderr="")),
        ("kill -0", ExecResult(exit_code=0, stdout="LIVE\n", stderr="")),
    ]
    rec = await b.start_background(
        "npm run dev", fs_write=_fsw(_ws()),
        network=NetworkConfig(), cwd=_WS, session_key="s1", project_id="p1")
    assert "VITE ready" in reg.read_log(rec["id"])
    assert reg.get(rec["id"]).port == 5173, reg.get(rec["id"]).port
    # Pulled, not appended: a second sync must not double the log.
    await b.sync_background_log(rec["id"])
    assert reg.read_log(rec["id"]).count("VITE ready") == 1
    print("OK log_sync_pulls_the_remote_tail_and_sniffs_the_port")


async def test_log_sync_marks_a_process_that_died_remotely():
    reg = _bg_registry("died")
    b, t = _backend()
    t.scripted = [
        ("setsid", ExecResult(exit_code=0, stdout="1500\n", stderr="")),
        ("tail -c", ExecResult(exit_code=0, stdout="crashed\n", stderr="")),
        ("kill -0", ExecResult(exit_code=1, stdout="", stderr="")),
    ]
    rec = await b.start_background(
        "npm run dev", fs_write=_fsw(_ws()),
        network=NetworkConfig(), cwd=_WS, session_key="s1", project_id="p1")
    assert reg.get(rec["id"]).status == "exited", reg.get(rec["id"]).status
    print("OK log_sync_marks_a_process_that_died_remotely")


def main():
    for t in (
        test_remote_workspace_skips_local_realpath,
        test_ensure_workspace_probes_and_mkdirs,
        test_ensure_workspace_unreachable_raises_capacity_error,
        test_exec_merges_runtime_env_and_passes_cwd,
        test_exec_cwd_outside_workspace_rejected,
        test_exec_transport_error_returns_failed_execresult,
        test_exec_stream_yields_live_then_result,
        test_file_io_round_trip_and_allow_paths,
        test_file_io_transport_error_maps_to_capacity_error,
        test_missing_file_raises_file_not_found,
        test_tools_resolve_is_lexical_for_remote_workspace,
        test_factory_env_dispatch,
        test_background_launch_captures_pgid_and_detaches,
        test_background_launch_failure_is_recorded_not_claimed_running,
        test_background_launch_when_host_unreachable,
        test_background_cwd_outside_workspace_never_reaches_the_host,
        test_terminate_kills_the_remote_GROUP,
        test_terminate_reports_failure_when_host_is_gone,
        test_local_registry_refuses_to_signal_a_remote_pgid,
        test_log_sync_pulls_the_remote_tail_and_sniffs_the_port,
        test_log_sync_marks_a_process_that_died_remotely,
    ):
        asyncio.run(t())
    print("\nall ssh-backend unit tests passed")


if __name__ == "__main__":
    main()
