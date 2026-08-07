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
import base64
import hashlib
import io
import logging
import os
import shlex
import tarfile
import threading
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
from ...config.schema import env_bool, env_int

if TYPE_CHECKING:
    from ..workspace import WorkspaceRoot

log = logging.getLogger(__name__)

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_USER = "1000:1000"

# HOME must be writable, and the image's own /home/sandbox is NOT: the rootfs
# is mounted read-only, so `uv` died with "failed to initialize cache at
# /home/sandbox/.cache/uv: read-only file system" and every skill script with
# it. LocalContainerBackend already had this lesson (_CONTAINER_HOME); this
# backend never got it.
#
# It lives INSIDE the workspace bind mount rather than on the /tmp tmpfs,
# which buys three things tmpfs cannot:
#   - disk-backed, so a multi-hundred-MB wheel download is not charged against
#     the container's memory limit,
#   - persistent, so the uv/pip cache survives the session — which is what the
#     old /root/.cache mount was reaching for, aimed at a HOME this container
#     does not use (it runs as uid 1000, not root),
#   - writable by `put_archive`, which refuses tmpfs destinations under a
#     read-only rootfs even though a shell can write them.
# `.adk-cc/` is already excluded from checkpoints, so the cache cannot leak
# into undo history.
CONTAINER_HOME = f"{CONTAINER_WORKSPACE}/.adk-cc/home"


def _network_mode() -> str:
    """Docker network for the sandbox. Default DENY, opt in via env.

    Deliberately stricter than LocalContainerBackend, which defaults network
    ON: that one is a developer's own machine, this one is a shared daemon
    serving multiple tenants, so egress is opt-in rather than opt-out. Set
    ADK_CC_SANDBOX_NETWORK=1 when the sandbox legitimately needs to reach a
    database, an internal API, or a package index — without it `pip install`,
    `uv`, and every outbound connection fail with no route.
    """
    return "bridge" if env_bool("ADK_CC_SANDBOX_NETWORK") else "none"


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
        # Production-only: when ensure_workspace gets a WorkspaceRoot with
        # a session_scratch_path set, this signals "we're under
        # TenantContext.workspace() — enable per-user install cache mount."
        # None / False for the dev path; the cache mount is skipped there
        # because dev is single-user and ~/.cache lives on the host.
        self._is_per_user_layout: bool = False
        # Built LAZILY (off the event loop) on first use — _build_client with
        # version="auto" does a blocking /version handshake to the daemon, which
        # must never run on the request loop. See _get_client.
        self._client = client
        self._container: Any = None  # docker.models.containers.Container
        self._lock = asyncio.Lock()
        self._client_lock = threading.Lock()

    # --- helpers ---------------------------------------------------------

    @property
    def _container_name(self) -> str:
        # docker container names: [a-zA-Z0-9][a-zA-Z0-9_.-]
        safe = "".join(c if c.isalnum() or c in "_.-" else "-" for c in self._session_id)
        return f"adk-cc-{safe}"

    def container_cwd(self, host_abs_path: str) -> str:
        # The host workspace root is bind-mounted at CONTAINER_WORKSPACE, so
        # that's what `pwd` returns and where absolute paths must live.
        return CONTAINER_WORKSPACE

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

    def _get_client(self) -> "docker.DockerClient":
        """Build the docker client lazily and OFF the event loop (the
        version="auto" daemon handshake blocks). MUST only be called from inside
        asyncio.to_thread; double-checked + thread-locked so a build runs once."""
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = _build_client()
        return self._client

    async def _ensure_container(self) -> Any:
        async with self._lock:
            # Build the client off-loop on first use (the /version handshake).
            await asyncio.to_thread(self._get_client)
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
                    # Adopting by NAME alone silently pins the config the
                    # container was born with. Turning on
                    # ADK_CC_SANDBOX_NETWORK then appears to do nothing,
                    # because network_mode is fixed at creation and the old
                    # container just gets reused — the operator changes a
                    # setting, restarts, and sees the identical failure.
                    # LocalContainerBackend already compares a signature for
                    # exactly this; this backend adopted blindly.
                    want = self._config_signature()
                    got = (c.labels or {}).get("adk-cc-config")
                    if got == want:
                        self._container = c
                        return c
                    log.info(
                        "sandbox config changed (%s -> %s); recreating %s",
                        got or "unlabelled", want, self._container_name)
                    try:
                        await asyncio.to_thread(c.remove, force=True, v=True)
                    except Exception:  # noqa: BLE001 — recreate regardless
                        pass

            self._container = await asyncio.to_thread(self._spawn_container)
            return self._container

    def _config_signature(self) -> str:
        """Everything fixed at CREATION time, so a change forces a recreate.

        Deliberately only creation-time settings: env and secrets are injected
        per exec and must NOT churn containers, while image / network / mount
        cannot be changed on a live container at all.
        """
        parts = (
            os.environ.get("ADK_CC_SANDBOX_IMAGE", "adk-cc-sandbox:latest"),
            _network_mode(),
            self._workspace_abs_path or "",
        )
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    def _spawn_container(self) -> Any:
        if self._workspace_abs_path is None:
            raise RuntimeError(
                "DockerBackend has no workspace path set. Call "
                "ensure_workspace(ws) before exec/read/write, or pass "
                "workspace_abs_path to the constructor."
            )
        image = os.environ.get("ADK_CC_SANDBOX_IMAGE", "adk-cc-sandbox:latest")
        mem_limit = os.environ.get("ADK_CC_SANDBOX_MEM_LIMIT", "4g")
        cpu_quota = env_int("ADK_CC_SANDBOX_CPU_QUOTA", 100000)
        # 512 matches the local_container backend and the schema default —
        # one PID cap for the same concept across backends (was 256 here).
        pids_limit = env_int("ADK_CC_SANDBOX_PIDS_LIMIT", 512)

        volumes = {
            self._workspace_abs_path: {
                "bind": CONTAINER_WORKSPACE,
                "mode": "rw",
            },
        }
        # NOTE: there used to be a per-user install-cache mount here, binding
        # <workspace>/.cache to /root/.cache. It never did anything: the
        # container runs as uid 1000, whose HOME is not /root, so neither uv
        # nor pip ever looked there. CONTAINER_HOME now lives inside the
        # workspace mount, which delivers the same persistence for every
        # layout without a second mount or a per-layout branch.

        return self._client.containers.run(
            image=image,
            detach=True,
            tty=True,
            name=self._container_name,
            network_mode=_network_mode(),
            mem_limit=mem_limit,
            cpu_quota=cpu_quota,
            pids_limit=pids_limit,
            read_only=True,
            tmpfs={"/tmp": "size=1g,mode=1777"},
            volumes=volumes,
            working_dir=CONTAINER_WORKSPACE,
            user=CONTAINER_USER,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            labels={
                "adk-cc-session": self._session_id,
                "adk-cc-tenant": self._tenant_id,
                # Read back on adoption; a mismatch forces a recreate.
                "adk-cc-config": self._config_signature(),
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
        await asyncio.to_thread(self._get_client)  # build client off-loop
        self._workspace_abs_path = ws.abs_path
        self._tenant_id = ws.tenant_id
        # Track whether we're in the production per-user layout so
        # _spawn_container can decide whether to bind-mount the install
        # cache. session_scratch_path is the marker — it's only set by
        # TenantContext.workspace() (production), never by default_workspace
        # (dev).
        self._is_per_user_layout = ws.session_scratch_path is not None
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

        # Build the mkdir target list. In production we also mkdir the
        # scratch path (so the per-session bind-mount target exists) and
        # the .cache dir owned by the runtime user (so /root/.cache
        # writes from inside the container don't permission-fail).
        targets = [ws.abs_path]
        if ws.session_scratch_path:
            targets.append(ws.session_scratch_path)
        if (
            self._is_per_user_layout
            and not env_bool("ADK_CC_DISABLE_INSTALL_CACHE_MOUNT")
        ):
            targets.append(os.path.join(ws.abs_path, ".cache"))

        # Single shell pipeline: mkdir all + chown the user-scoped paths
        # to the runtime user so the hardened container's user can write.
        # The container runs as `0:0` for this helper only — exec lifecycle
        # is detach=False, remove=True, so it's gone before the next call.
        chown_target = ws.abs_path
        shell = (
            "mkdir -p " + " ".join(shlex.quote(p) for p in targets)
            + " && chown -R 1000:1000 " + shlex.quote(chown_target)
        )
        try:
            await asyncio.to_thread(
                self._client.containers.run,
                image=image,
                command=["sh", "-c", shell],
                remove=True,
                detach=False,
                user="0:0",  # root inside the helper, scoped to mkdir+chown
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
        # On-demand secret/env injection (user secrets + operator SandboxEnvSpec),
        # resolved fresh per exec. Without this, API keys never reach the remote
        # container's commands. NOTE: docker-py puts these in the ExecCreate API
        # payload (readable via `docker inspect <exec-id>` on the daemon for the
        # container's life) — a hygiene step below LocalContainerBackend's
        # name-only `-e KEY` forwarding, but acceptable for the remote deployment
        # where the daemon host is trusted infrastructure, not the user's laptop.
        runtime_env = await self._runtime_env()
        # HOME first, so an operator-supplied HOME in ADK_CC_SANDBOX_ENV still
        # wins. Everything that caches — uv, pip, npm — derives its path from
        # this, and the image's own HOME is on the read-only rootfs.
        env = {"HOME": CONTAINER_HOME, **(runtime_env or {})}

        def _run() -> ExecResult:
            try:
                # Create HOME per exec rather than once at container start: the
                # backend can adopt a container from a previous boot, and a
                # workspace can be re-created under it, so "once" is not a
                # guarantee. mkdir -p is idempotent and costs a syscall.
                rc, output = container.exec_run(
                    cmd=["bash", "-lc",
                         f"mkdir -p {shlex.quote(env['HOME'])} 2>/dev/null; {cmd}"],
                    workdir=cwd_in_container,
                    user=CONTAINER_USER,
                    environment=env,
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
            try:
                ok = container.put_archive(path=target_dir, data=buf.getvalue())
                if not ok:
                    raise IOError(
                        f"put_archive returned False for {path_in_container}")
                return
            except docker.errors.APIError as e:
                # Docker's archive API refuses ANY destination when the rootfs
                # is read-only — including /tmp, which is a tmpfs a shell in
                # this same container writes happily. Measured: put_archive to
                # /workspace succeeds, to /tmp returns 400 "container rootfs is
                # marked read-only", while `echo > /tmp/f` works in both.
                # Surfaced to users as a raw 400 from write_file.
                #
                # So fall back to the path that demonstrably works. Restricted
                # to the read-only complaint on purpose: a genuine permission
                # or quota error must still fail loudly rather than be retried
                # into a confusing second error.
                if "read-only" not in str(e).lower():
                    raise
                b64 = base64.b64encode(encoded).decode("ascii")
                rc, out = container.exec_run(
                    cmd=["bash", "-lc",
                         f"printf %s {shlex.quote(b64)} | base64 -d > "
                         f"{shlex.quote(path_in_container)}"],
                    user=CONTAINER_USER,
                )
                if rc != 0:
                    raise IOError(
                        f"write to {path_in_container} failed after the "
                        f"read-only rootfs fallback: "
                        f"{(out or b'').decode('utf-8', 'replace')[:300]}"
                    ) from e

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
