"""Docker-based sandbox backend.

Connects to a (typically remote) Docker daemon and runs each session
inside its own container. Mirrors the SandboxBackend ABC; tools don't
know about Docker.

Topology assumed by this backend:

  agent process                            sandbox host (Linux VM)
  ─────────────────────────                ────────────────────────
  DockerBackend(client) ──── tcp://… ────► Docker daemon
                            (plain or                │
                             mTLS)                   ▼
                                          adk-cc-sandbox container
                                          + bind-mounted /workspace
                                          + read-only rootfs
                                          + tmpfs /tmp
                                          + network=none (default)
                                          + mem/cpu/pids limits

The backend never assumes the agent has local Docker access. Workspace
files live on the sandbox host's filesystem; the agent reaches them
exclusively through `read_text` / `write_text` / `exec`.

Connection mode is picked by env vars:
  - `ADK_CC_DOCKER_HOST` — required. Examples:
        unix:///var/run/docker.sock  (local socket)
        tcp://sandbox.internal:2375  (plain TCP — trusted internal LAN)
        tcp://sandbox.internal:2376  (TLS TCP — also set the three CERT vars)
  - `ADK_CC_DOCKER_CA_CERT`,
    `ADK_CC_DOCKER_CLIENT_CERT`,
    `ADK_CC_DOCKER_CLIENT_KEY` — optional. If all three are set, mTLS
    is enabled. Otherwise plain (or unix-socket) connection.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shlex
import tarfile
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Optional

import docker

from ..config import (
    ExecResult,
    FsReadConfig,
    FsWriteConfig,
    NetworkConfig,
    SandboxViolation,
)
from .base import SandboxBackend

if TYPE_CHECKING:
    from ..workspace import WorkspaceRoot

log = logging.getLogger(__name__)

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_USER = "1000:1000"


def _build_client() -> docker.DockerClient:
    base_url = os.environ.get("ADK_CC_DOCKER_HOST")
    if not base_url:
        # Fall back to the env-var contract docker-py recognises.
        # If neither is set, this raises a clear DockerException.
        return docker.from_env(version="auto", timeout=30)

    ca = os.environ.get("ADK_CC_DOCKER_CA_CERT")
    cert = os.environ.get("ADK_CC_DOCKER_CLIENT_CERT")
    key = os.environ.get("ADK_CC_DOCKER_CLIENT_KEY")

    if ca and cert and key:
        log.info("DockerBackend: connecting to %s with mTLS", base_url)
        tls = docker.tls.TLSConfig(
            client_cert=(cert, key),
            ca_cert=ca,
            verify=True,
        )
        return docker.DockerClient(
            base_url=base_url, tls=tls, version="auto", timeout=30
        )

    log.info("DockerBackend: connecting to %s without TLS", base_url)
    return docker.DockerClient(base_url=base_url, version="auto", timeout=30)


class DockerBackend(SandboxBackend):
    """One container per session, lifecycle tied to the session."""

    name = "docker"

    def __init__(
        self,
        *,
        session_id: str = "local",
        tenant_id: str = "local",
        workspace_abs_path: Optional[str] = None,
        client: Optional[docker.DockerClient] = None,
    ) -> None:
        self._session_id = session_id
        self._tenant_id = tenant_id
        # Workspace path on the SANDBOX HOST (not the agent pod).
        # Set by ensure_workspace when called via the tenancy plugin.
        self._workspace_abs_path: Optional[str] = workspace_abs_path
        self._client = client or _build_client()
        self._container: Any = None  # docker.models.containers.Container
        self._lock = asyncio.Lock()

    # --- helpers ---------------------------------------------------------

    @property
    def _container_name(self) -> str:
        # docker container names: [a-zA-Z0-9][a-zA-Z0-9_.-]
        safe = "".join(c if c.isalnum() or c in "_.-" else "-" for c in self._session_id)
        return f"adk-cc-{safe}"

    def _to_container_path(self, host_path: str) -> str:
        """Translate a sandbox-host path to the container's /workspace path."""
        if self._workspace_abs_path is None:
            # Without an explicit workspace, treat host_path as already
            # container-relative or absolute. Useful for ad-hoc exec calls.
            return host_path
        ws = self._workspace_abs_path.rstrip("/")
        if host_path == ws:
            return CONTAINER_WORKSPACE
        if host_path.startswith(ws + "/"):
            tail = host_path[len(ws) + 1:]
            return str(PurePosixPath(CONTAINER_WORKSPACE) / tail)
        # Path outside the workspace mount — pass through unchanged. The
        # rootfs is read-only; reads of /etc/passwd etc. still work but
        # writes will fail.
        return host_path

    async def _ensure_container(self) -> Any:
        async with self._lock:
            if self._container is not None:
                # Refresh state in case Docker reaped the container.
                try:
                    await asyncio.to_thread(self._container.reload)
                    if self._container.status == "running":
                        return self._container
                except Exception:
                    pass
                self._container = None

            # Try to find a still-running container from a previous boot
            # (agent pod restart with same session). Otherwise create.
            existing = await asyncio.to_thread(
                self._client.containers.list, all=True,
                filters={"name": self._container_name},
            )
            if existing:
                c = existing[0]
                if c.status != "running":
                    try:
                        await asyncio.to_thread(c.start)
                    except Exception:
                        await asyncio.to_thread(c.remove, v=True)
                        c = None
                if c is not None:
                    self._container = c
                    return c

            self._container = await asyncio.to_thread(self._spawn_container)
            return self._container

    def _spawn_container(self) -> Any:
        if self._workspace_abs_path is None:
            raise RuntimeError(
                "DockerBackend has no workspace path set. Call "
                "ensure_workspace(ws) before exec/read/write, or pass "
                "workspace_abs_path to the constructor."
            )
        image = os.environ.get("ADK_CC_SANDBOX_IMAGE", "adk-cc-sandbox:latest")
        mem_limit = os.environ.get("ADK_CC_SANDBOX_MEM_LIMIT", "4g")
        cpu_quota = int(os.environ.get("ADK_CC_SANDBOX_CPU_QUOTA", "100000"))
        pids_limit = int(os.environ.get("ADK_CC_SANDBOX_PIDS_LIMIT", "256"))
        return self._client.containers.run(
            image=image,
            detach=True,
            tty=True,
            name=self._container_name,
            network_mode="none",
            mem_limit=mem_limit,
            cpu_quota=cpu_quota,
            pids_limit=pids_limit,
            read_only=True,
            tmpfs={"/tmp": "size=1g,mode=1777"},
            volumes={
                self._workspace_abs_path: {
                    "bind": CONTAINER_WORKSPACE,
                    "mode": "rw",
                },
            },
            working_dir=CONTAINER_WORKSPACE,
            user=CONTAINER_USER,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            labels={
                "adk-cc-session": self._session_id,
                "adk-cc-tenant": self._tenant_id,
            },
            command=["sleep", "infinity"],
        )

    # --- ABC methods -----------------------------------------------------

    async def ensure_workspace(self, ws: "WorkspaceRoot") -> None:
        """Create the workspace dir on the sandbox host.

        The agent pod can't `mkdir` on the remote VM directly, so we
        run a one-shot helper container that bind-mounts the workspace
        parent and creates the dir from inside.
        """
        self._workspace_abs_path = ws.abs_path
        self._tenant_id = ws.tenant_id
        # If the per-session container is already up, the bind mount
        # already covers this. Nothing to do.
        if self._container is not None:
            return

        # Use a throwaway alpine/busybox container to create the dir on
        # the sandbox host. Bind-mount the parent so we can create the
        # leaf directory. We use the configured sandbox image so we
        # don't introduce a new image dependency.
        parent = os.path.dirname(ws.abs_path.rstrip("/"))
        if not parent:
            return
        image = os.environ.get("ADK_CC_SANDBOX_IMAGE", "adk-cc-sandbox:latest")
        cmd = ["mkdir", "-p", ws.abs_path]
        try:
            await asyncio.to_thread(
                self._client.containers.run,
                image=image,
                command=cmd,
                remove=True,
                detach=False,
                user="0:0",  # root inside the helper container, scoped to mkdir
                volumes={parent: {"bind": parent, "mode": "rw"}},
                # No network or other privileges needed.
                network_mode="none",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
            )
        except Exception as e:
            log.warning("ensure_workspace via helper container failed: %s", e)

    async def exec(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,
        network: NetworkConfig,
        timeout_s: int,
        cwd: str,
    ) -> ExecResult:
        container = await self._ensure_container()
        cwd_in_container = self._to_container_path(cwd) if cwd else CONTAINER_WORKSPACE

        def _run() -> ExecResult:
            try:
                rc, output = container.exec_run(
                    cmd=["bash", "-lc", cmd],
                    workdir=cwd_in_container,
                    user=CONTAINER_USER,
                    demux=True,
                )
            except Exception as e:
                return ExecResult(exit_code=-1, stdout="", stderr=f"{type(e).__name__}: {e}")
            stdout_b, stderr_b = output if isinstance(output, tuple) else (output, b"")
            return ExecResult(
                exit_code=int(rc) if rc is not None else -1,
                stdout=(stdout_b or b"").decode("utf-8", errors="replace"),
                stderr=(stderr_b or b"").decode("utf-8", errors="replace"),
            )

        try:
            return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s)
        except asyncio.TimeoutError:
            return ExecResult(
                exit_code=-1, stdout="", stderr=f"timed out after {timeout_s}s",
                timed_out=True,
            )

    async def read_text(self, path: str, *, fs_read: FsReadConfig) -> str:
        if not fs_read.allows(path):
            raise SandboxViolation(f"read denied by fs_read: {path}")
        container = await self._ensure_container()
        path_in_container = self._to_container_path(path)

        def _read() -> bytes:
            rc, output = container.exec_run(
                cmd=["cat", path_in_container],
                user=CONTAINER_USER,
                demux=True,
            )
            stdout_b, stderr_b = output if isinstance(output, tuple) else (output, b"")
            if rc != 0:
                err = (stderr_b or b"").decode("utf-8", errors="replace")
                if "No such file" in err:
                    raise FileNotFoundError(path)
                raise IOError(f"read failed (exit {rc}): {err}")
            return stdout_b or b""

        data = await asyncio.to_thread(_read)
        return data.decode("utf-8")

    async def write_text(
        self, path: str, content: str, *, fs_write: FsWriteConfig
    ) -> None:
        if not fs_write.allows(path):
            raise SandboxViolation(f"write denied by fs_write: {path}")
        container = await self._ensure_container()
        path_in_container = self._to_container_path(path)
        target_dir = str(PurePosixPath(path_in_container).parent)
        target_name = PurePosixPath(path_in_container).name
        encoded = content.encode("utf-8")

        # Build a tar stream containing one file with the target name,
        # then put_archive into the parent dir. put_archive extracts
        # tar entries relative to the destination path.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=target_name)
            info.size = len(encoded)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(encoded))
        buf.seek(0)

        def _write() -> None:
            # Ensure the target dir exists inside the bind mount.
            container.exec_run(
                cmd=["mkdir", "-p", target_dir], user=CONTAINER_USER
            )
            ok = container.put_archive(path=target_dir, data=buf.getvalue())
            if not ok:
                raise IOError(f"put_archive returned False for {path_in_container}")

        await asyncio.to_thread(_write)

    async def close(self) -> None:
        if self._container is None:
            return
        c = self._container
        self._container = None
        try:
            await asyncio.to_thread(c.stop, timeout=2)
        except Exception:
            pass
        try:
            await asyncio.to_thread(c.remove, v=True)
        except Exception:
            pass
