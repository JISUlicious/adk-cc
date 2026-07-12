"""Local container sandbox — runtime detection + backend selection + spawn-arg
construction. Pure/mocked (no daemon needed); the live container run is in
tests/e2e_container_sandbox.py.

Run: PYTHONPATH=agents .venv/bin/python tests/test_container_sandbox.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.sandbox.backends import container_runtime as cr
from adk_cc.sandbox.backends.container_runtime import Runtime
from adk_cc.sandbox.backends.local_container_backend import LocalContainerBackend, _safe_name
from adk_cc.sandbox.backends.noop_backend import NoopBackend
from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig


class _FakeProc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


def _patch_detect(monkey: dict, *, which: dict, versions: dict):
    """Install fake shutil.which + subprocess.run into container_runtime.
    `which` maps name→path|None; `versions` maps name→server-version|"" (down)."""
    cr.reset_cache()
    orig_which, orig_run = cr.shutil.which, cr.subprocess.run
    monkey["restore"] = lambda: (setattr(cr.shutil, "which", orig_which),
                                 setattr(cr.subprocess, "run", orig_run),
                                 cr.reset_cache())
    cr.shutil.which = lambda name: which.get(name)

    def fake_run(args, **kw):
        name = os.path.basename(args[0])
        ver = versions.get(name, "")
        return _FakeProc(rc=0 if ver else 1, stdout=(ver + "\n") if ver else "")

    cr.subprocess.run = fake_run


def test_detect_prefers_docker_then_podman():
    m = {}
    _patch_detect(m, which={"docker": "/bin/docker", "podman": "/bin/podman"},
                  versions={"docker": "28.3.0", "podman": "5.0"})
    try:
        rt = cr.detect_runtime()
        assert rt is not None and rt.name == "docker" and rt.version == "28.3.0"
    finally:
        m["restore"]()


def test_detect_falls_through_to_podman_when_no_docker():
    m = {}
    _patch_detect(m, which={"podman": "/bin/podman"}, versions={"podman": "5.0"})
    try:
        rt = cr.detect_runtime()
        assert rt is not None and rt.name == "podman"
    finally:
        m["restore"]()


def test_detect_none_when_daemon_down():
    m = {}
    # binary present but `version` returns empty (daemon unreachable)
    _patch_detect(m, which={"docker": "/bin/docker"}, versions={})
    try:
        assert cr.detect_runtime() is None
    finally:
        m["restore"]()


def test_detect_none_when_nothing_installed():
    m = {}
    _patch_detect(m, which={}, versions={})
    try:
        assert cr.detect_runtime() is None
    finally:
        m["restore"]()


def test_detect_respects_runtime_env_override():
    m = {}
    _patch_detect(m, which={"docker": "/bin/docker", "podman": "/bin/podman"},
                  versions={"docker": "28", "podman": "5"})
    os.environ["ADK_CC_SANDBOX_RUNTIME"] = "podman"
    try:
        cr.reset_cache()
        rt = cr.detect_runtime()
        assert rt is not None and rt.name == "podman"
    finally:
        del os.environ["ADK_CC_SANDBOX_RUNTIME"]
        m["restore"]()


def test_detect_is_cached_until_reset():
    m = {}
    _patch_detect(m, which={"docker": "/bin/docker"}, versions={"docker": "28"})
    try:
        assert cr.detect_runtime().name == "docker"
        # flip the daemon "down" — cached result should persist until reset
        cr.subprocess.run = lambda *a, **k: _FakeProc(rc=1, stdout="")
        assert cr.detect_runtime().name == "docker"  # cache hit
        cr.reset_cache()
        assert cr.detect_runtime() is None  # re-probed → down
    finally:
        m["restore"]()


def test_selection_precedence():
    from adk_cc import deployment

    saved = {k: os.environ.get(k) for k in
             ("ADK_CC_SANDBOX_BACKEND", "ADK_CC_DESKTOP", "ADK_CC_SANDBOX_MODE")}
    orig_avail = deployment.container_runtime_available
    try:
        for k in saved:
            os.environ.pop(k, None)
        deployment.container_runtime_available = lambda: True

        # explicit env always wins
        os.environ["ADK_CC_SANDBOX_BACKEND"] = "daytona"
        assert deployment.sandbox_backend_name() == "daytona"
        del os.environ["ADK_CC_SANDBOX_BACKEND"]

        # desktop + container-mode + runtime available → container
        os.environ["ADK_CC_DESKTOP"] = "1"
        os.environ["ADK_CC_SANDBOX_MODE"] = "container"
        assert deployment.sandbox_backend_name() == "container"

        # ...but noop if no runtime
        deployment.container_runtime_available = lambda: False
        assert deployment.sandbox_backend_name() == "noop"
        deployment.container_runtime_available = lambda: True

        # desktop + host-mode → noop
        os.environ["ADK_CC_SANDBOX_MODE"] = "host"
        assert deployment.sandbox_backend_name() == "noop"

        # non-desktop → noop regardless of mode
        del os.environ["ADK_CC_DESKTOP"]
        os.environ["ADK_CC_SANDBOX_MODE"] = "container"
        assert deployment.sandbox_backend_name() == "noop"
    finally:
        deployment.container_runtime_available = orig_avail
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_backend_is_noop_subclass_with_host_direct_io():
    # File I/O must be inherited host-direct (not containerized) for in-place.
    b = LocalContainerBackend(session_id="s1")
    assert isinstance(b, NoopBackend)
    assert b.name == "container"
    # the read/write methods are NoopBackend's (host-direct), not overridden
    assert type(b).read_text is NoopBackend.read_text
    assert type(b).write_text is NoopBackend.write_text
    # but exec IS overridden (containerized)
    assert type(b).exec is not NoopBackend.exec


def test_spawn_args_identical_path_mount_and_ownership():
    b = LocalContainerBackend(session_id="sess/ab.1", workspace_abs_path="/tmp")
    b._mounts = ["/Users/me/proj", "/Users/me/data"]
    assert _safe_name("sess/ab.1") == "adk-cc-sess-ab.1"
    # identical-path bind mounts (host:host), never a /workspace remap
    assert b._mount_args() == [
        "-v", "/Users/me/proj:/Users/me/proj:rw",
        "-v", "/Users/me/data:/Users/me/data:rw",
    ]
    # docker → explicit --user uid:gid; podman → keep-id
    docker_own = b._ownership_args(Runtime("docker", "28", "/bin/docker"))
    assert docker_own[:1] == ["--user"] and ":" in docker_own[1]
    assert b._ownership_args(Runtime("podman", "5", "/bin/podman")) == ["--userns=keep-id"]
    # hardening present
    limits = b._limit_args()
    assert "no-new-privileges" in limits and "--cap-drop" in limits and "--pids-limit" in limits


def test_sandbox_settings_persistence_and_precedence():
    import tempfile
    from adk_cc import deployment

    d = tempfile.mkdtemp(prefix="sbx-set-")
    saved_env = {k: os.environ.get(k) for k in
                 ("ADK_CC_DESKTOP_DATA", "ADK_CC_SANDBOX_MODE",
                  "ADK_CC_SANDBOX_NETWORK", "ADK_CC_SANDBOX_IMAGE")}
    try:
        os.environ["ADK_CC_DESKTOP_DATA"] = d
        for k in ("ADK_CC_SANDBOX_MODE", "ADK_CC_SANDBOX_NETWORK", "ADK_CC_SANDBOX_IMAGE"):
            os.environ.pop(k, None)

        # defaults with no file
        assert deployment.sandbox_mode() == "host"
        assert deployment.sandbox_network_enabled() is True
        assert deployment.sandbox_image() == "python:3.12-slim"

        # persisted values are read back
        deployment.write_sandbox_settings({"mode": "container", "network": False, "image": "custom:1"})
        assert deployment.read_sandbox_settings()["mode"] == "container"
        assert deployment.sandbox_mode() == "container"
        assert deployment.sandbox_network_enabled() is False
        assert deployment.sandbox_image() == "custom:1"

        # env overrides the stored value
        os.environ["ADK_CC_SANDBOX_MODE"] = "host"
        os.environ["ADK_CC_SANDBOX_NETWORK"] = "1"
        assert deployment.sandbox_mode() == "host"
        assert deployment.sandbox_network_enabled() is True
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_exec_forwards_env_by_name_not_value():
    # Secret VALUES must ride the CLI subprocess env (referenced by `-e NAME`),
    # never appear as literal argv (which would leak into `ps`/shell history).
    import asyncio

    b = LocalContainerBackend(session_id="s", runtime=Runtime("docker", "28", "/bin/docker"))
    b._started = True  # skip container create
    b._workspace_abs = "/tmp"

    captured = {}

    async def fake_env():
        return {"MY_API_KEY": "sk-topsecret", "REGION": "us"}

    b._runtime_env = fake_env  # type: ignore[method-assign]

    def fake_to_thread(fn, *a, **k):
        return asyncio.sleep(0, result=fn(*a, **k))

    import adk_cc.sandbox.backends.local_container_backend as mod
    orig_to_thread = mod.asyncio.to_thread
    orig_run = mod.subprocess.run
    mod.asyncio.to_thread = fake_to_thread

    class _P:
        returncode, stdout, stderr = 0, "", ""

    def cap_run(args, **kw):
        captured["args"] = args
        captured["env"] = kw.get("env") or {}
        return _P()

    mod.subprocess.run = cap_run
    try:
        asyncio.run(b.exec("echo hi", fs_write=FsWriteConfig(), network=NetworkConfig(),
                           timeout_s=5, cwd="/tmp"))
    finally:
        mod.asyncio.to_thread = orig_to_thread
        mod.subprocess.run = orig_run

    argv = captured["args"]
    # names are on argv as `-e NAME`; VALUES are NOT anywhere on argv
    assert "-e" in argv and "MY_API_KEY" in argv and "REGION" in argv
    assert "sk-topsecret" not in argv, "secret value must not appear on argv"
    assert not any("sk-topsecret" in str(a) for a in argv)
    # the value IS in the subprocess env (that's how `-e NAME` forwards it)
    assert captured["env"].get("MY_API_KEY") == "sk-topsecret"
    # the in-container timeout wrapper is present
    assert "timeout" in argv and "5" in argv


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__[5:]}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__[5:]}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__[5:]}: {type(e).__name__}: {e}")
    print("\nall container-sandbox unit tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
