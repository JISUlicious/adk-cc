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


class SandboxViolation(Exception):
    """Raised by a backend when an operation is blocked by config.

    Tools catch this and surface a structured error to the LLM rather than
    crashing.
    """
