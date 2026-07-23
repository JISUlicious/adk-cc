"""File-based session store for desktop mode — per-project JSONL under $HOME.

A custom ADK ``BaseSessionService`` that keeps session history as human-readable,
per-project files instead of a single opaque sqlite DB. This is the desktop
counterpart to the in-place workspace: history lives *locally, per project*, so
it is greppable, portable, and self-contained (zip/delete a project folder and
its history goes with it).

Layout under ``<base>`` (the desktop data dir, NOT the project repo — the agent
works in the repo in-place, so keeping transcripts out of it avoids the agent
grepping / checkpointing its own history)::

    <base>/app_state.json                          # app: state  (global, shared)
    <base>/projects/<user_id>/user_state.json      # user: state (shared across the project's sessions)
    <base>/projects/<user_id>/sessions/<id>.jsonl  # header line + one JSON line per Event

In desktop mode ``user_id`` IS the project id, so ``projects/<user_id>/`` is the
per-project folder.

State scoping mirrors ADK's model (see InMemorySessionService): ``app:`` / ``user:``
keys are shared side-state (routed to the json side-files so every session of a
project sees the current values — e.g. ``user:adk_cc_allow_rules``, the persisted
"allow-always" permission decisions), session-scoped keys live in the per-session
file, and ``temp:`` is never persisted. On read we replay a session's own events
for its session-scoped state, then overlay the current app/user state on top.

Injected via ADK's service registry under the ``adkccfiles://`` URI scheme
(``register_file_session_scheme``), so it drops in wherever ADK resolves
``session_service_uri`` — no fork.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.events.event import Event
from google.adk.sessions import _session_util
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.session import Session
from google.adk.sessions.state import State

_log = logging.getLogger(__name__)

_SCHEME = "adkccfiles"


def _safe(value: str, label: str) -> str:
    """Reject an id that isn't plain alnum/-/_ — these become path segments, so a
    crafted app_name/user_id/session_id must never escape the base dir."""
    v = "".join(c for c in (value or "") if c.isalnum() or c in "-_")
    if not v or v != value:
        raise ValueError(f"unsafe {label}: {value!r}")
    return v


def _is_user_text(event: Event) -> bool:
    """True if `event` is a real user message (author user + a text part) — i.e. it
    STARTS a logical turn. A user function_response (a HITL answer) is author-user
    but carries only a functionResponse part, so it does NOT count."""
    content = getattr(event, "content", None)
    if getattr(event, "author", None) != "user" and getattr(content, "role", None) != "user":
        return False
    for p in getattr(content, "parts", None) or []:
        if getattr(p, "text", None) and not getattr(p, "thought", None):
            return True
    return False


class FileSessionService(BaseSessionService):
    """Sessions persisted as per-project JSONL files (see module docstring)."""

    def __init__(self, base_dir: str | os.PathLike) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        # Per-session locks serialize event appends; one lock guards the shared
        # app/user state files. Single-process desktop, but async → serialize.
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._shared_lock = asyncio.Lock()

    # --- paths ---------------------------------------------------------

    def _project_dir(self, user_id: str) -> Path:
        return self._base / "projects" / _safe(user_id, "user_id")

    def _sessions_dir(self, user_id: str) -> Path:
        return self._project_dir(user_id) / "sessions"

    def _session_file(self, user_id: str, session_id: str) -> Path:
        return self._sessions_dir(user_id) / f"{_safe(session_id, 'session_id')}.jsonl"

    def _user_state_file(self, user_id: str) -> Path:
        return self._project_dir(user_id) / "user_state.json"

    def _app_state_file(self) -> Path:
        return self._base / "app_state.json"

    # --- json helpers --------------------------------------------------

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)  # atomic within the same dir

    def _merge_app_state(self, delta: dict[str, Any]) -> None:
        cur = self._read_json(self._app_state_file())
        cur.update(delta)
        self._write_json(self._app_state_file(), cur)

    def _merge_user_state(self, user_id: str, delta: dict[str, Any]) -> None:
        cur = self._read_json(self._user_state_file(user_id))
        cur.update(delta)
        self._write_json(self._user_state_file(user_id), cur)

    # --- shared user-state accessors (used by the desktop working-dirs API) ---

    def get_user_value(self, user_id: str, key: str, default: Any = None) -> Any:
        """Read a shared `user:`-scoped value (unprefixed key) for a project.
        Overlaid onto every future session of that project via `_merge_state_view`."""
        return self._read_json(self._user_state_file(user_id)).get(key, default)

    def set_user_value(self, user_id: str, key: str, value: Any) -> None:
        """Write a shared `user:`-scoped value (unprefixed key) for a project."""
        self._merge_user_state(user_id, {key: value})

    def _merge_state_view(self, user_id: str, session: Session) -> Session:
        """Overlay current shared app/user state (prefixed) onto session.state —
        ADK's merged view. Shared values win over any session-historical copy."""
        for k, v in self._read_json(self._app_state_file()).items():
            session.state[State.APP_PREFIX + k] = v
        for k, v in self._read_json(self._user_state_file(user_id)).items():
            session.state[State.USER_PREFIX + k] = v
        return session

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    # --- file (de)serialization ---------------------------------------

    def _read_file(self, path: Path) -> tuple[dict[str, Any], list[Event]]:
        """Return (header, events) parsed from a session JSONL file."""
        header: dict[str, Any] = {}
        events: list[Event] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("kind") == "session":
                        header = rec
                    elif rec.get("kind") == "event":
                        try:
                            events.append(Event.model_validate(rec["event"]))
                        except Exception as e:  # noqa: BLE001 — skip a corrupt line, keep the rest
                            _log.warning("skip unparsable event in %s: %s", path.name, e)
        except OSError:
            pass
        return header, events

    def _session_scoped_state(self, header: dict[str, Any], events: list[Event]) -> dict[str, Any]:
        """Rebuild the session-scoped state: the header's initial state + replay of
        each event's session-scoped (non app/user/temp) delta."""
        state: dict[str, Any] = dict(header.get("state") or {})
        for ev in events:
            delta = ev.actions.state_delta if ev.actions else None
            if not delta:
                continue
            for k, v in delta.items():
                if (
                    not k.startswith(State.APP_PREFIX)
                    and not k.startswith(State.USER_PREFIX)
                    and not k.startswith(State.TEMP_PREFIX)
                ):
                    state[k] = v
        return state

    # --- BaseSessionService contract ----------------------------------

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        deltas = _session_util.extract_state_delta(state or {})
        async with self._shared_lock:
            if deltas["app"]:
                self._merge_app_state(deltas["app"])
            if deltas["user"]:
                self._merge_user_state(user_id, deltas["user"])

        sid = session_id.strip() if session_id and session_id.strip() else uuid.uuid4().hex
        path = self._session_file(user_id, sid)
        if session_id and path.exists():
            raise AlreadyExistsError(f"Session with id {sid} already exists.")

        now = time.time()
        header = {
            "kind": "session",
            "app_name": app_name,
            "user_id": user_id,
            "id": sid,
            "state": deltas["session"],
            "create_time": now,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")

        session = Session(
            app_name=app_name,
            user_id=user_id,
            id=sid,
            state=dict(deltas["session"]),
            last_update_time=now,
        )
        return self._merge_state_view(user_id, session)

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        path = self._session_file(user_id, session_id)
        if not path.exists():
            return None
        header, events = self._read_file(path)
        if header.get("app_name") and header.get("app_name") != app_name:
            return None  # defensive: session belongs to a different app

        state = self._session_scoped_state(header, events)
        last_ts = events[-1].timestamp if events else header.get("create_time") or time.time()

        # Event filtering (state above is always from the FULL history).
        out_events = events
        if config:
            if config.num_recent_events is not None:
                out_events = [] if config.num_recent_events == 0 else events[-config.num_recent_events:]
            if config.after_timestamp:
                i = len(out_events) - 1
                while i >= 0 and out_events[i].timestamp >= config.after_timestamp:
                    i -= 1
                out_events = out_events[i + 1:]

        session = Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=state,
            events=out_events,
            last_update_time=last_ts,
        )
        return self._merge_state_view(user_id, session)

    async def list_sessions(
        self, *, app_name: str, user_id: Optional[str] = None
    ) -> ListSessionsResponse:
        out: list[Session] = []
        if user_id is not None:
            user_ids = [user_id]
        else:
            projects = self._base / "projects"
            user_ids = [p.name for p in projects.iterdir()] if projects.is_dir() else []

        for uid in user_ids:
            sdir = self._sessions_dir(uid)
            if not sdir.is_dir():
                continue
            for path in sdir.glob("*.jsonl"):
                header, events = self._read_file(path)
                if header.get("app_name") and header.get("app_name") != app_name:
                    continue
                last_ts = events[-1].timestamp if events else header.get("create_time") or 0.0
                # list returns sessions WITHOUT events (ADK contract).
                session = Session(
                    app_name=app_name,
                    user_id=uid,
                    id=header.get("id") or path.stem,
                    state=self._session_scoped_state(header, events),
                    last_update_time=last_ts,
                )
                out.append(self._merge_state_view(uid, session))
        return ListSessionsResponse(sessions=out)

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        try:
            self._session_file(user_id, session_id).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    async def truncate_before_invocation(
        self, *, user_id: str, session_id: str, invocation_id: str
    ) -> int:
        """Drop all events from `invocation_id` onward (the turn a checkpoint
        preceded, plus everything after) — rewinding the CONVERSATION to before
        that turn, to match a file rewind. Returns the number of events kept, or
        -1 if the session/file wasn't found. Best-effort + atomic (tmp + replace)."""
        if not invocation_id:
            return -1
        try:
            path = self._session_file(user_id, session_id)
        except ValueError:
            return -1
        if not path.exists():
            return -1
        header, events = self._read_file(path)
        cut = next(
            (i for i, ev in enumerate(events) if getattr(ev, "invocation_id", None) == invocation_id),
            None,
        )
        if cut is None:
            return len(events)  # that invocation isn't here → nothing to truncate
        # A logical turn can span several invocations: when it pauses for a HITL
        # answer (ask_user_question / a confirmation), the answer arrives as a NEW
        # invocation whose first event is a user function_response, not a user text
        # message. Rewinding should undo the whole ask→answer→act sequence, so cut
        # at the user TEXT message that started the turn — walk back from the target
        # invocation's first event to the most recent real user message.
        for i in range(cut, -1, -1):
            if _is_user_text(events[i]):
                cut = i
                break
        kept = events[:cut]
        async with self._lock_for(session_id):
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                if header:
                    f.write(json.dumps(header) + "\n")
                for ev in kept:
                    f.write(json.dumps({"kind": "event", "event": ev.model_dump(mode="json")}) + "\n")
            os.replace(tmp, path)
        return len(kept)

    async def delete_last_event(
        self, *, user_id: str, session_id: str, event_id: str
    ) -> bool:
        """Delete the session's LAST event — only when its id matches
        `event_id` (the caller states what it believes is last; a mismatch
        means someone appended meanwhile and nothing is touched). Used by the
        Turn Broker to prune the orphaned user message of an errored
        zero-output turn (F2c) before re-running it. Atomic (tmp + replace).
        Returns True when the event was deleted."""
        try:
            path = self._session_file(user_id, session_id)
        except ValueError:
            return False
        if not path.exists() or not event_id:
            return False
        async with self._lock_for(session_id):
            header, events = self._read_file(path)
            if not events or getattr(events[-1], "id", None) != event_id:
                return False
            kept = events[:-1]
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                if header:
                    f.write(json.dumps(header) + "\n")
                for ev in kept:
                    f.write(json.dumps({"kind": "event", "event": ev.model_dump(mode="json")}) + "\n")
            os.replace(tmp, path)
        return True

    async def append_event(self, session: Session, event: Event) -> Event:
        if event.partial:
            return event
        # Base does: apply temp to in-memory state, trim temp from the event,
        # update session.state with the (non-temp) delta, append to session.events.
        await super().append_event(session=session, event=event)
        session.last_update_time = event.timestamp

        # Persist the event line (temp already trimmed by super()).
        path = self._session_file(session.user_id, session.id)
        async with self._lock_for(session.id):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "event", "event": event.model_dump(mode="json")}) + "\n")

        # Route app:/user: deltas to the shared side-files (session-scoped keys
        # stay recoverable by replaying this event line).
        if event.actions and event.actions.state_delta:
            d = _session_util.extract_state_delta(event.actions.state_delta)
            if d["app"] or d["user"]:
                async with self._shared_lock:
                    if d["app"]:
                        self._merge_app_state(d["app"])
                    if d["user"]:
                        self._merge_user_state(session.user_id, d["user"])
        return event


def register_file_session_scheme() -> None:
    """Register the ``adkccfiles://`` session-service scheme with ADK's registry.

    Idempotent. After this, ``session_service_uri='adkccfiles://<base-dir>'`` (or a
    bare ``adkccfiles://`` → the desktop data dir) resolves to a FileSessionService.
    """
    from google.adk.cli.service_registry import get_service_registry

    def _factory(uri: str, **_kwargs: Any) -> FileSessionService:
        base = urlparse(uri).path or ""
        if not base or base == "/":
            from .. import deployment

            base = str(deployment.desktop_data_dir())
        return FileSessionService(base_dir=base)

    get_service_registry().register_session_service(_SCHEME, _factory)
