"""Shared harness: a throwaway REAL sshd in Docker for the ssh e2e suite.

Boots `lscr.io/linuxserver/openssh-server` on a loopback port with a
generated ed25519 keypair (key auth only — mirrors the product's
no-password policy) and hands back everything a test needs to build an
`SshTransport` against it. Used by e2e_ssh_transport / e2e_ssh_backend and
the later panel/checkpoint e2es, so container boot logic lives once.

Usage:
    from sshd_harness import SshdContainer

    with SshdContainer() as box:   # None → caller should SKIP (no Docker)
        t = SshTransport(box.host, port=box.port,
                         identity_file=box.identity_file,
                         extra_ssh_opts=box.extra_ssh_opts,
                         control_dir=box.control_dir)
        ...

`SshdContainer()` returns a context manager; `__enter__` yields None when
Docker (or the image, offline) is unavailable so callers can print a SKIP
and exit 0. Teardown always removes the container and the temp dir.

Host-key handling is TEST-scoped: a throwaway UserKnownHostsFile +
StrictHostKeyChecking=accept-new via extra opts — production inherits the
user's own known_hosts and never relaxes checking.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

IMAGE = "lscr.io/linuxserver/openssh-server:latest"
USER = "dev"
_BASE_PORT = 42219


def _sh(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kw)


def docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    return _sh(["docker", "version", "--format", "{{.Server.Version}}"]).returncode == 0


@dataclass
class SshdBox:
    host: str
    port: int
    identity_file: str
    extra_ssh_opts: tuple[str, ...]
    control_dir: str
    container_id: str
    tmp: str
    # The image's fixed home dir for USER (linuxserver convention).
    home: str = "/config"


@dataclass
class SshdContainer:
    """Context manager owning one sshd container + throwaway keypair."""

    port: int = field(default_factory=lambda: _BASE_PORT + (os.getpid() % 200))
    _box: SshdBox | None = None

    def __enter__(self) -> SshdBox | None:
        if not docker_ready():
            print("[SKIP] docker daemon not available — start Docker Desktop and rerun")
            return None
        tmp = tempfile.mkdtemp(prefix="adk-sshd-")
        key = os.path.join(tmp, "id_ed25519")
        if _sh(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", key]).returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError("ssh-keygen failed")
        pub = open(key + ".pub", encoding="utf-8").read().strip()

        if _sh(["docker", "image", "inspect", IMAGE]).returncode != 0:
            print(f"[sshd-harness] pulling {IMAGE} …")
            if _sh(["docker", "pull", IMAGE], timeout=300).returncode != 0:
                print("[SKIP] could not pull sshd image (offline?)")
                shutil.rmtree(tmp, ignore_errors=True)
                return None

        run = _sh(
            [
                "docker", "run", "-d", "--rm",
                "-p", f"127.0.0.1:{self.port}:2222",
                "-e", f"PUBLIC_KEY={pub}",
                "-e", f"USER_NAME={USER}",
                IMAGE,
            ]
        )
        if run.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"docker run failed: {run.stderr}")

        known_hosts = os.path.join(tmp, "known_hosts")
        self._box = SshdBox(
            host=f"{USER}@127.0.0.1",
            port=self.port,
            identity_file=key,
            extra_ssh_opts=(
                "-o", f"UserKnownHostsFile={known_hosts}",
                "-o", "StrictHostKeyChecking=accept-new",
            ),
            control_dir=os.path.join(tmp, "ctl"),
            container_id=run.stdout.strip(),
            tmp=tmp,
        )
        return self._box

    def __exit__(self, *exc) -> None:
        if self._box:
            _sh(["docker", "rm", "-f", self._box.container_id])
            shutil.rmtree(self._box.tmp, ignore_errors=True)


async def wait_ready(transport, *, timeout_s: float = 90.0) -> str | None:
    """Poll `run('echo ready')` until sshd accepts the key. Returns None on
    success, else the last error string (caller fails the test with it)."""
    import asyncio

    from adk_cc.sandbox.ssh_transport import SshConnectionError

    deadline = time.monotonic() + timeout_s
    last: str | None = "no attempt"
    while time.monotonic() < deadline:
        try:
            res = await transport.run("echo ready", timeout_s=10)
            if res.exit_code == 0 and "ready" in res.stdout:
                return None
            last = f"exit={res.exit_code} err={res.stderr[:120]}"
        except SshConnectionError as e:
            last = str(e)[:160]
        await asyncio.sleep(2)
    return last
