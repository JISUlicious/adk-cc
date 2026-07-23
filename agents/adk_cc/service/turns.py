"""Turn Broker — durable runs decoupled from the SSE consumer.

ADK's `/run_sse` executes the run INSIDE the response generator, so a client
disconnect (timeout, tab close, refresh) cancels the turn mid-flight (F1).
The broker inverts that: a turn runs as a server-side asyncio task to
completion regardless of subscribers; streaming becomes a TAIL of the turn's
event buffer, re-attachable with a cursor. See
analysis/durable-runs-design.md.

Also closes, at the broker layer:
  - F3 (server half): a run that ends on a dangling `_handback_to_coordinator`
    (a confirmation answered inside a sub-agent roots the resumed run there;
    the specialist's handback marker then dangles) is auto-continued — bounded
    — so the coordinator gets its turn for EVERY driver, not just the web UI.
  - F2b: `retry_last` re-runs the last errored turn's original message.
  - Error classification: terminal errors carry the rate-limit class from
    `models/rate_limit.py` so UIs can render "retry" vs "switch model".

Deliberate v1 bounds (design doc): per-process (desktop and the dev web
deployment are single-worker); turns do not survive a server restart (session
events do); ADK's own `/run_sse` is untouched as a fallback path.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Callable, Optional

_log = logging.getLogger(__name__)

# Finished turns are kept for reattach/inspection this long, then dropped.
_FINISHED_TTL_S = 600.0
# Bounded F3 auto-continues per turn — a guard, not a loop.
_MAX_CONTINUES = 2
_CONTINUE_TEXT = "Continue."

_HANDBACK = "_handback_to_coordinator"


def _is_dangling_handback(event: Any) -> bool:
    """True when `event` carries the specialist's synthetic handback marker
    (a `_handback_to_coordinator` function call)."""
    content = getattr(event, "content", None)
    for p in getattr(content, "parts", None) or []:
        fc = getattr(p, "function_call", None)
        if fc is not None and getattr(fc, "name", None) == _HANDBACK:
            return True
    return False


def _has_model_text(event: Any) -> bool:
    """A model-authored, non-partial event with visible (non-thought) text —
    i.e. an actual reply the user sees."""
    if getattr(event, "author", "user") == "user" or getattr(event, "partial", False):
        return False
    content = getattr(event, "content", None)
    for p in getattr(content, "parts", None) or []:
        if getattr(p, "text", None) and not getattr(p, "thought", False):
            return True
    return False


def _classify_error(err: BaseException) -> dict[str, Any]:
    """Terminal-error payload for the tail/status endpoints."""
    from ..models.rate_limit import classify_429
    from ..models.selectable import _is_rate_limited

    payload: dict[str, Any] = {
        "type": type(err).__name__,
        "message": str(err)[:500],
        "rate_limited": False,
    }
    cause = err.__cause__ or err
    if _is_rate_limited(cause) or _is_rate_limited(err):
        kind, hint = classify_429(cause)
        payload.update({"rate_limited": True, "kind": kind, "reset_hint_s": hint})
    elif "quota exhausted" in str(err):
        payload.update({"rate_limited": True, "kind": "quota"})
    return payload


class Turn:
    """One durable run: an owning task, an event buffer, and subscribers."""

    def __init__(self, *, app_name: str, user_id: str, session_id: str,
                 new_message: Any, state_delta: Optional[dict] = None) -> None:
        self.id = f"turn_{uuid.uuid4().hex[:12]}"
        self.app_name = app_name
        self.user_id = user_id
        self.session_id = session_id
        self.new_message = new_message      # kept for retry_last
        self.state_delta = state_delta
        self.status = "running"             # running | done | error | aborted
        self.error: Optional[dict[str, Any]] = None
        self.events: list[str] = []         # serialized SSE payloads, cursor = index
        self.model_events = 0               # model-authored events (F2c signal)
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.task: Optional[asyncio.Task] = None
        self._cond = asyncio.Condition()

    # -- event flow -----------------------------------------------------

    async def push(self, payload: str, *, model_authored: bool) -> None:
        async with self._cond:
            self.events.append(payload)
            if model_authored:
                self.model_events += 1
            self._cond.notify_all()

    async def finish(self, status: str, error: Optional[dict] = None) -> None:
        async with self._cond:
            self.status = status
            self.error = error
            self.finished_at = time.time()
            self._cond.notify_all()

    async def tail(self, cursor: int = 0) -> AsyncGenerator[str, None]:
        """Yield serialized events from `cursor`; return when the turn ends
        and the buffer is drained. Safe to abandon at any point — the turn
        does not care about its subscribers."""
        i = max(0, cursor)
        while True:
            async with self._cond:
                while i >= len(self.events) and self.status == "running":
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=15.0)
                    except asyncio.TimeoutError:
                        break  # let the caller emit a keepalive
                chunk = self.events[i:]
                done = self.status != "running"
            for payload in chunk:
                yield payload
            i += len(chunk)
            if done and i >= len(self.events):
                return
            if not chunk:
                yield ""  # keepalive marker (router turns it into a comment)

    def snapshot(self) -> dict[str, Any]:
        return {
            "turn_id": self.id,
            "status": self.status,
            "cursor": len(self.events),
            "model_events": self.model_events,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class TurnBroker:
    """Owns turn tasks. Single-flight per session; finished turns GC'd."""

    def __init__(self, *, get_runner: Callable[[str], Any],
                 session_service: Any) -> None:
        self._get_runner = get_runner          # async: app_name -> Runner
        self.session_service = session_service
        self._turns: dict[str, Turn] = {}
        self._latest_by_session: dict[tuple[str, str, str], str] = {}

    # -- lookup ---------------------------------------------------------

    def get(self, turn_id: str) -> Optional[Turn]:
        return self._turns.get(turn_id)

    def latest_for(self, app_name: str, user_id: str, session_id: str) -> Optional[Turn]:
        tid = self._latest_by_session.get((app_name, user_id, session_id))
        return self._turns.get(tid) if tid else None

    # -- lifecycle ------------------------------------------------------

    def _gc(self) -> None:
        now = time.time()
        dead = [tid for tid, t in self._turns.items()
                if t.finished_at and now - t.finished_at > _FINISHED_TTL_S]
        for tid in dead:
            t = self._turns.pop(tid)
            key = (t.app_name, t.user_id, t.session_id)
            if self._latest_by_session.get(key) == tid:
                self._latest_by_session.pop(key, None)

    def start(self, *, app_name: str, user_id: str, session_id: str,
              new_message: Any, state_delta: Optional[dict] = None) -> Turn:
        """Begin a durable turn. Raises RuntimeError when the session already
        has a running turn (single-flight)."""
        self._gc()
        cur = self.latest_for(app_name, user_id, session_id)
        if cur is not None and cur.status == "running":
            raise RuntimeError(f"session busy: turn {cur.id} is running")
        turn = Turn(app_name=app_name, user_id=user_id, session_id=session_id,
                    new_message=new_message, state_delta=state_delta)
        self._turns[turn.id] = turn
        self._latest_by_session[(app_name, user_id, session_id)] = turn.id
        turn.task = asyncio.create_task(self._drive(turn), name=f"adk-cc-{turn.id}")

        # Safety net: a task cancelled BEFORE its first step never runs the
        # coroutine body at all — _drive's own except-handler can't record the
        # terminal state. The done-callback closes that gap (and any exception
        # that somehow escapes _drive).
        def _on_done(t: asyncio.Task, _turn: Turn = turn) -> None:
            if _turn.status != "running":
                return
            if t.cancelled():
                asyncio.ensure_future(_turn.finish("aborted"))
                return
            exc = t.exception()
            err = _classify_error(exc) if exc else {
                "type": "unknown", "message": "task ended abnormally",
                "rate_limited": False,
            }
            asyncio.ensure_future(_turn.finish("error", err))

        turn.task.add_done_callback(_on_done)
        return turn

    async def abort(self, turn_id: str) -> bool:
        t = self._turns.get(turn_id)
        if t is None or t.task is None or t.status != "running":
            return False
        t.task.cancel()
        return True

    def retry_last(self, *, app_name: str, user_id: str, session_id: str) -> Turn:
        """Re-run the latest turn's ORIGINAL message (F2b). Only for turns
        that ended in error. NOTE (F2c): the errored attempt's user event may
        still sit in history; pruning lands with the session-service delete
        support — until then the model sees a repeated user message, which is
        exactly what manual re-sending caused anyway."""
        last = self.latest_for(app_name, user_id, session_id)
        if last is None:
            raise LookupError("no previous turn for this session")
        if last.status == "running":
            raise RuntimeError(f"session busy: turn {last.id} is running")
        if last.status != "error":
            raise LookupError(f"last turn ended '{last.status}' — nothing to retry")
        return self.start(app_name=app_name, user_id=user_id,
                          session_id=session_id, new_message=last.new_message,
                          state_delta=last.state_delta)

    # -- the run itself -------------------------------------------------

    async def _drive(self, turn: Turn) -> None:
        try:
            runner = await self._get_runner(turn.app_name)
            message = turn.new_message
            for round_ in range(1 + _MAX_CONTINUES):
                # F3 detection: the round needs a continuation when a handback
                # marker appears with NO model text after it. Covers both
                # shapes: non-resumable (marker IS the last event) and
                # resumable (ADK ends a resumed parent right after the
                # sub-agent — the marker is followed only by its auto-response
                # and end-of-agent checkpoints, never a reply).
                dangling = False
                saw_any = False
                async for event in runner.run_async(
                    user_id=turn.user_id,
                    session_id=turn.session_id,
                    new_message=message,
                    state_delta=turn.state_delta if round_ == 0 else None,
                ):
                    saw_any = True
                    if _is_dangling_handback(event):
                        dangling = True
                    elif _has_model_text(event):
                        dangling = False
                    authored = getattr(event, "author", "user") != "user"
                    await turn.push(
                        event.model_dump_json(exclude_none=True, by_alias=True),
                        model_authored=authored,
                    )
                if not saw_any or not dangling:
                    break
                # F3 (server half): the resumed run ended on the specialist's
                # dangling handback — continue so the coordinator replies.
                _log.info("turn %s: dangling handback — auto-continuing (%d)",
                          turn.id, round_ + 1)
                from google.genai import types as _t

                message = _t.Content(role="user",
                                     parts=[_t.Part(text=_CONTINUE_TEXT)])
            await turn.finish("done")
        except asyncio.CancelledError:
            await turn.finish("aborted")
            # swallow: cancellation is the abort endpoint's expected path
        except Exception as e:  # noqa: BLE001 — terminal state must be recorded
            _log.warning("turn %s failed: %s: %s", turn.id, type(e).__name__,
                         str(e)[:300])
            await turn.finish("error", _classify_error(e))


# -- wiring ------------------------------------------------------------


def extract_adk_web_server(app: Any) -> Optional[Any]:
    """Find the AdkWebServer instance behind a get_fast_api_app() app.

    ADK registers routes as LOCAL functions inside
    `AdkWebServer.get_fast_api_app`, so each endpoint closes over `self` —
    scan endpoints' closure cells (unwrapping decorators) for it. Bound-method
    endpoints (`__self__`) are also handled in case ADK refactors. Returns
    None — the caller degrades gracefully — if the internals shifted."""
    def _probe(fn: Any) -> Optional[Any]:
        seen = 0
        while hasattr(fn, "__wrapped__") and seen < 8:
            owner = _cells(fn)
            if owner is not None:
                return owner
            fn = fn.__wrapped__
            seen += 1
        return _cells(fn)

    def _cells(fn: Any) -> Optional[Any]:
        owner = getattr(fn, "__self__", None)
        if owner is not None and type(owner).__name__ == "AdkWebServer":
            return owner
        for cell in getattr(fn, "__closure__", None) or ():
            try:
                v = cell.cell_contents
            except ValueError:  # empty cell
                continue
            if type(v).__name__ == "AdkWebServer":
                return v
        return None

    for route in getattr(app, "routes", []):
        fn = getattr(route, "endpoint", None)
        if fn is None:
            continue
        owner = _probe(fn)
        if owner is not None:
            return owner
    return None
