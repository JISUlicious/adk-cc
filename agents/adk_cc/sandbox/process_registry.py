"""Long-running background processes: the registry that makes them visible.

A backgrounded process is deliberately OUTSIDE the turn's lifetime — which
means it also escapes every cleanup the turn has. `start_new_session=True`
puts it in its own process group, so it survives the backend dying too. That
is exactly the orphan class #98 fixed for the app→backend relationship, one
level down, so ownership is designed in here rather than bolted on:

  * every process records its pgid at launch (pgid == pid: the child is its
    own group leader), which is what makes a reliable group kill possible;
  * the index is PERSISTED, so a backend restart can tell "still running and
    mine" from "gone" instead of silently forgetting;
  * `sweep()` at boot reconciles the index against reality.

Logs are FILES, not memory ring buffers: they must outlive the turn, the
session, and a backend restart, which is the entire point of the feature. A
memory buffer dies with the process that owns it — fine for a 30s command,
wrong here.

Policy (surfaced in the UI): a background process survives turns and
sessions, but NOT the app quitting. Anything else is a footgun on a desktop
app — a server nobody remembers starting, holding a port after the app is
gone.
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import logging

_log = logging.getLogger(__name__)

# Per-process log cap. Generous (a dev server logs every request) but bounded:
# the file is truncated from the FRONT when it exceeds this, because the tail
# is what tells you why something just broke.
_MAX_LOG_BYTES = 8 * 1024 * 1024
_TRIM_TO_BYTES = 4 * 1024 * 1024

_STATUSES = ("starting", "running", "exited", "killed", "failed", "unknown")

# `Listening on http://localhost:5173`, `:8000`, `port 3000` — best-effort
# only; a wrong guess must never become a broken link, so we require the
# port to look like a port and keep the first plausible hit.
_PORT_RE = re.compile(
    r"(?:https?://[\w.\-]*:(\d{2,5})\b)"
    r"|(?:\blocalhost:(\d{2,5})\b)"
    r"|(?:\b(?:port|PORT)\s*[:=]?\s*(\d{2,5})\b)"
)


@dataclass
class ProcessRecord:
    id: str
    session_key: str
    project_id: str
    label: str
    command: str
    cwd: str
    backend: str
    log_path: str
    pid: Optional[int] = None
    pgid: Optional[int] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    status: str = "starting"
    exit_code: Optional[int] = None
    port: Optional[int] = None
    can_terminate: bool = True
    # True once a sweep found it running but no longer owned by this backend
    # generation (adopted rather than spawned).
    adopted: bool = False
    # REMOTE backends only: the log lives on the other machine and is pulled
    # on demand (a second long-lived channel per process would be worse).
    remote_log_path: str = ""

    def elapsed_s(self) -> float:
        """Wall-clock age: how long it ran, or has been running."""
        return (self.finished_at or time.time()) - self.started_at

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["elapsed_s"] = round(self.elapsed_s(), 1)
        return d


class ProcessRegistry:
    """Process-global registry, persisted under the data dir."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = self.root / "processes"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        self._records: dict[str, ProcessRecord] = {}
        self._load()

    # ---- persistence -------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.index_path.read_text("utf-8"))
        except Exception:  # noqa: BLE001 — a missing/corrupt index is not fatal
            return
        for item in raw if isinstance(raw, list) else []:
            try:
                rec = ProcessRecord(**item)
            except Exception:  # noqa: BLE001 — skip records from a newer schema
                continue
            self._records[rec.id] = rec

    def _save(self) -> None:
        try:
            tmp = self.index_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps([asdict(r) for r in self._records.values()],
                           indent=1),
                encoding="utf-8")
            tmp.replace(self.index_path)
        except Exception as e:  # noqa: BLE001 — never break a turn over the index
            _log.warning("process index write failed: %s", e)

    # ---- lifecycle ---------------------------------------------------
    def create(self, *, session_key: str, project_id: str, label: str,
               command: str, cwd: str, backend: str,
               can_terminate: bool = True) -> ProcessRecord:
        pid_ = uuid.uuid4().hex[:12]
        rec = ProcessRecord(
            id=pid_, session_key=session_key, project_id=project_id,
            label=label or _label_from_command(command),
            command=_redact(command), cwd=cwd, backend=backend,
            log_path=str(self.dir / f"{pid_}.log"),
            can_terminate=can_terminate,
        )
        # Port from the COMMAND, immediately: `--port 5173`, `-p 3000`,
        # `http.server 8000`. Log-sniffing (below) is better when the server
        # announces itself, but many do so only after a slow boot — and a dev
        # server whose port appears a minute late is a link the user already
        # gave up on.
        rec.port = _port_from_command(command)
        self._records[pid_] = rec
        Path(rec.log_path).touch()
        self._save()
        return rec

    def mark_started(self, pid_: str, *, pid: int, pgid: int) -> None:
        rec = self._records.get(pid_)
        if not rec:
            return
        rec.pid, rec.pgid, rec.status = pid, pgid, "running"
        self._save()

    def mark_exited(self, pid_: str, *, exit_code: Optional[int],
                    status: str = "exited") -> None:
        rec = self._records.get(pid_)
        if not rec:
            return
        rec.exit_code = exit_code
        rec.status = status if status in _STATUSES else "exited"
        rec.finished_at = time.time()
        self._save()

    def append_log(self, pid_: str, data: bytes) -> None:
        rec = self._records.get(pid_)
        if not rec or not data:
            return
        p = Path(rec.log_path)
        try:
            with p.open("ab") as fh:
                fh.write(data)
            if p.stat().st_size > _MAX_LOG_BYTES:
                self._trim(p)
        except Exception as e:  # noqa: BLE001
            _log.debug("log append failed for %s: %s", pid_, e)
        if rec.port is None:
            self._sniff_port(rec, data)

    @staticmethod
    def _trim(p: Path) -> None:
        """Keep the TAIL: what a log is for is explaining what just happened."""
        try:
            with p.open("rb") as fh:
                fh.seek(-_TRIM_TO_BYTES, os.SEEK_END)
                tail = fh.read()
            p.write_bytes(b"[... earlier output trimmed ...]\n" + tail)
        except Exception:  # noqa: BLE001
            pass

    def _sniff_port(self, rec: ProcessRecord, data: bytes) -> None:
        try:
            m = _PORT_RE.search(data.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return
        if not m:
            return
        for g in m.groups():
            if not g:
                continue
            n = int(g)
            if 1 <= n <= 65535:
                rec.port = n
                self._save()
            return

    def save(self) -> None:
        """Persist the index (callers that mutate a record in place)."""
        self._save()

    def replace_log(self, pid_: str, data: bytes) -> None:
        """Overwrite the local log with a remote tail. Remote logs are PULLED,
        so appending would duplicate everything already fetched."""
        rec = self._records.get(pid_)
        if not rec:
            return
        try:
            Path(rec.log_path).write_bytes(data)
        except OSError as e:
            _log.debug("log replace failed for %s: %s", pid_, e)
        if rec.port is None:
            self._sniff_port(rec, data)

    def read_log(self, pid_: str, *, tail_bytes: int = 64_000) -> str:
        rec = self._records.get(pid_)
        if not rec:
            return ""
        try:
            p = Path(rec.log_path)
            size = p.stat().st_size
            with p.open("rb") as fh:
                if size > tail_bytes:
                    fh.seek(size - tail_bytes)
                return fh.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""

    # ---- queries -----------------------------------------------------
    def get(self, pid_: str) -> Optional[ProcessRecord]:
        return self._records.get(pid_)

    def list(self, *, project_id: Optional[str] = None,
             session_key: Optional[str] = None) -> list[ProcessRecord]:
        """Running first, then most recently finished.

        Scoped by PROJECT by default, not session: a dev server started in one
        session is still what occupies the port in the next one, and hiding it
        per-session is how a forgotten process stays forgotten.
        """
        rows = list(self._records.values())
        if project_id:
            rows = [r for r in rows if r.project_id == project_id]
        if session_key:
            rows = [r for r in rows if r.session_key == session_key]
        rows.sort(key=lambda r: (r.status not in ("running", "starting"),
                                 -(r.finished_at or r.started_at)))
        return rows

    def forget(self, pid_: str) -> bool:
        """Drop a FINISHED record from the list (the log file stays)."""
        rec = self._records.get(pid_)
        if not rec or rec.status in ("running", "starting"):
            return False
        self._records.pop(pid_, None)
        self._save()
        return True

    # ---- ownership ---------------------------------------------------
    def sweep(self) -> dict[str, int]:
        """Reconcile the index against reality at boot.

        Three outcomes per record, and the third is why this exists: a process
        recorded as running that is STILL alive belongs to a previous backend
        generation. Reporting it (rather than silently forgetting, or silently
        adopting) is #98's lesson — a stale thing holding a port must be
        visible and killable, not a mystery.
        """
        stats = {"alive": 0, "gone": 0}
        for rec in self._records.values():
            if rec.status not in ("running", "starting"):
                continue
            if rec.pgid and _pgid_alive(rec.pgid):
                rec.adopted = True
                stats["alive"] += 1
            else:
                rec.status = "unknown"
                rec.finished_at = rec.finished_at or time.time()
                stats["gone"] += 1
        if stats["alive"] or stats["gone"]:
            _log.info("process sweep: %d still running (adopted), %d gone",
                      stats["alive"], stats["gone"])
            self._save()
        return stats

    def terminate(self, pid_: str, *, grace_s: float = 3.0) -> bool:
        """TERM the process GROUP, grace, then KILL — the same discipline the
        timeout path already uses, because killing only the shell leaves the
        real child (the server) alive holding the port."""
        rec = self._records.get(pid_)
        if not rec or not rec.pgid:
            return False
        if rec.status not in ("running", "starting"):
            return True
        if rec.backend != "noop":
            # A remote pgid means nothing to THIS host — signalling it locally
            # would either fail or, worse, hit an unrelated local process.
            # The owning backend does it over its own channel.
            _log.debug("terminate(%s): backend %r must handle this remotely",
                       pid_, rec.backend)
            return False
        _signal_group(rec.pgid, signal.SIGTERM)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if not _pgid_alive(rec.pgid):
                self.mark_exited(pid_, exit_code=None, status="killed")
                return True
            time.sleep(0.1)
        _signal_group(rec.pgid, signal.SIGKILL)
        self.mark_exited(pid_, exit_code=None, status="killed")
        return True

    def terminate_all(self, *, project_id: Optional[str] = None,
                      session_key: Optional[str] = None) -> int:
        n = 0
        for rec in self.list(project_id=project_id, session_key=session_key):
            if rec.status in ("running", "starting") and self.terminate(rec.id):
                n += 1
        return n


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        # PermissionError = exists but not ours; treat as gone for ownership
        # purposes rather than claiming a process we cannot signal.
        return False
    except Exception:  # noqa: BLE001
        return False


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except Exception as e:  # noqa: BLE001
        _log.debug("killpg(%s, %s) failed: %s", pgid, sig, e)


def _label_from_command(cmd: str) -> str:
    """A human label when the caller gave none: the first meaningful token."""
    parts = [p for p in (cmd or "").split() if not p.startswith(("ADK_", "-"))]
    return " ".join(parts[:3])[:60] or "process"


_CMD_PORT_RE = re.compile(
    r"(?:--port[=\s]+(\d{2,5}))"
    r"|(?:\s-p[=\s]+(\d{2,5})\b)"
    r"|(?:http\.server\s+(\d{2,5}))"
    r"|(?::(\d{2,5})\b)")


def _port_from_command(cmd: str) -> Optional[int]:
    m = _CMD_PORT_RE.search(cmd or "")
    if not m:
        return None
    for g in m.groups():
        if g and 1 <= int(g) <= 65535:
            return int(g)
    return None


# The key must END at the secret word: a trailing [A-Z0-9_]* made "PATH="
# match on "PAT" and redact the interpreter path out of every recorded
# command (seen on the first live run — the UI showed `export PATH=***`).
_SECRET_RE = re.compile(
    r"\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|_PAT|CREDENTIAL))"
    r"\s*=\s*(\S+)")


def _redact(cmd: str) -> str:
    """Commands carry secrets (`FOO_TOKEN=… npm run deploy`). The registry is
    displayed and persisted, so redact before either — but ONLY the value:
    over-redaction makes the recorded command unreadable, which defeats the
    point of showing it at all."""
    return _SECRET_RE.sub(r"\1=***", cmd or "")


# ---- process-global accessor -----------------------------------------
_registry: Optional[ProcessRegistry] = None


def get_registry() -> ProcessRegistry:
    global _registry
    if _registry is None:
        from ..service.desktop_routes import desktop_data_dir

        try:
            root = desktop_data_dir()
        except Exception:  # noqa: BLE001 — non-desktop deployments
            root = Path(os.environ.get("ADK_CC_DATA_DIR")
                        or os.path.expanduser("~/.adk-cc"))
        _registry = ProcessRegistry(Path(root))
        _registry.sweep()
    return _registry


def _reset_for_test(root: Optional[Path] = None) -> ProcessRegistry:
    global _registry
    _registry = ProcessRegistry(root) if root else None
    return _registry  # type: ignore[return-value]
