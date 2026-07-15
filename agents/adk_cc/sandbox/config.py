"""Sandbox restriction configs and result types.

The configs are passed per-call by the tool layer; the backend honors
them. They're declarative — backends translate them into bind mounts /
seccomp rules / iptables / e2b API calls / etc. The noop backend honors
them via path/host string checks so the contract is exercised end-to-end
even in dev.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FsReadConfig:
    """Read-side filesystem policy.

    `allow_paths` is a list of glob patterns. A read is allowed if the
    resolved absolute path matches ANY pattern. An empty list means deny
    all reads.
    """

    allow_paths: tuple[str, ...] = ()

    def allows(self, abs_path: str) -> bool:
        return any(fnmatch.fnmatch(abs_path, p) for p in self.allow_paths)


@dataclass(frozen=True)
class FsWriteConfig:
    """Write-side filesystem policy."""

    allow_paths: tuple[str, ...] = ()

    def allows(self, abs_path: str) -> bool:
        return any(fnmatch.fnmatch(abs_path, p) for p in self.allow_paths)


@dataclass(frozen=True)
class NetworkConfig:
    """Network egress policy. Empty allow list = no network."""

    allow_domains: tuple[str, ...] = ()

    def allows(self, host: str) -> bool:
        return any(fnmatch.fnmatch(host, p) for p in self.allow_domains)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class ExecChunk:
    """One frame from a streaming exec.

    Backends that support live output (currently SandboxServiceBackend
    via SSE) yield one of these per chunk during `exec_stream(...)`.
    The final chunk in any stream has `kind="result"` and `result` set
    to the full `ExecResult` so callers don't need to reconstruct it
    from the stdout/stderr chunks.

    Backends without streaming infrastructure default to a single
    `kind="result"` chunk after the sync `exec` returns.
    """

    kind: str  # "stdout" | "stderr" | "result"
    data: str = ""
    result: "ExecResult | None" = None


class SandboxViolation(Exception):
    """Raised by a backend when an operation is blocked by config.

    Tools catch this and surface a structured error to the LLM rather than
    crashing.
    """


class SandboxCapacityError(SandboxViolation):
    """Transient backpressure from the sandbox backend — the operation
    hit a capacity limit or rate limit and should be *retried after a
    backoff*, not surfaced as a permanent failure.

    Subclasses SandboxViolation so a handler that only knows the broad
    type still catches it as a last resort (e.g. after a backend's own
    bounded retry is exhausted), while a backend that knows how to wait
    can catch this narrower type and back off.

    `retry_after` is the server-suggested minimum wait in seconds
    (parsed from a `Retry-After` / `X-RateLimit-Reset` header) when the
    response carried one, else None.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
