"""HTTP surface for the Turn Broker (durable runs) — see turns.py.

    POST /api/turns                     start a turn        → 409 when busy
    GET  /api/turns/{id}/stream?cursor  SSE tail, re-attachable
    GET  /api/turns/{id}                status snapshot
    POST /api/turns/{id}/abort          cancel the task
    GET  /api/turns/latest?…            session's latest turn (reconnect)
    POST /api/turns/retry-last          re-run the last errored turn (F2b)

Mounted by build_fastapi_app AFTER the auth middleware is installed, so the
same bearer/no-auth policy as every other route applies.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


def mount_turn_routes(app: Any, broker: Any) -> None:
    from google.genai import types

    def _content(body: dict) -> Any:
        raw = body.get("newMessage") or body.get("new_message")
        if not raw:
            raise HTTPException(status_code=400, detail="newMessage required")
        try:
            return types.Content.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"bad newMessage: {e}")

    def _ids(body: dict) -> tuple[str, str, str]:
        try:
            return (str(body["appName"]), str(body["userId"]),
                    str(body["sessionId"]))
        except KeyError as e:
            raise HTTPException(status_code=400,
                                detail=f"missing field: {e.args[0]}")

    @app.post("/api/turns", include_in_schema=False)
    async def start_turn(request: Request):
        body = await request.json()
        app_name, user_id, session_id = _ids(body)
        # 404 on unknown session — same contract as ADK's own run routes.
        session = await broker.session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        try:
            turn = broker.start(
                app_name=app_name, user_id=user_id, session_id=session_id,
                new_message=_content(body),
                state_delta=body.get("stateDelta"),
            )
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return JSONResponse(turn.snapshot(), status_code=201)

    @app.get("/api/turns/latest", include_in_schema=False)
    async def latest_turn(appName: str, userId: str, sessionId: str):
        turn = broker.latest_for(appName, userId, sessionId)
        if turn is None:
            raise HTTPException(status_code=404, detail="no turn for session")
        return turn.snapshot()

    @app.get("/api/turns/{turn_id}", include_in_schema=False)
    async def turn_status(turn_id: str):
        turn = broker.get(turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="unknown turn")
        return turn.snapshot()

    @app.post("/api/turns/{turn_id}/abort", include_in_schema=False)
    async def abort_turn(turn_id: str):
        if broker.get(turn_id) is None:
            raise HTTPException(status_code=404, detail="unknown turn")
        cancelled = await broker.abort(turn_id)
        return {"status": "aborting" if cancelled else "not_running"}

    @app.get("/api/turns/{turn_id}/stream", include_in_schema=False)
    async def stream_turn(turn_id: str, cursor: int = 0):
        turn = broker.get(turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="unknown turn")

        async def gen():
            async for payload in turn.tail(cursor):
                if payload == "":
                    yield ": keepalive\n\n"   # SSE comment — resets proxies' idle timers
                else:
                    yield f"data: {payload}\n\n"
            # terminal marker so clients need no extra status poll
            yield f"event: turn_end\ndata: {_end_payload(turn)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    def _end_payload(turn: Any) -> str:
        import json

        return json.dumps({"status": turn.status, "error": turn.error})

    @app.post("/api/turns/retry-last", include_in_schema=False)
    async def retry_last(request: Request):
        body = await request.json()
        app_name, user_id, session_id = _ids(body)
        try:
            turn = broker.retry_last(app_name=app_name, user_id=user_id,
                                     session_id=session_id)
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return JSONResponse(turn.snapshot(), status_code=201)
