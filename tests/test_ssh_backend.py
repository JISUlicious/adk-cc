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
    ):
        asyncio.run(t())
    print("\nall ssh-backend unit tests passed")


if __name__ == "__main__":
    main()
