"""The WebSocket endpoint.

Two tasks per client, deliberately:

* the **reader** handles ``subscribe`` / ``unsubscribe`` / ``ping``;
* the **writer** drains the conflating map at whatever speed this client manages.

Splitting them is what makes backpressure possible at all. A single loop that
both reads and writes couples the two: a client that stops reading also stops
being able to send an unsubscribe, and the server cannot tell "slow" from "gone".
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime

import pydantic
import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from fx_api.deps import get_manager, get_settings, get_tickets
from fx_api.ws.conflator import SLOW_DISCONNECTS, ClientSession
from fx_api.ws.protocol import (
    MAX_SYMBOLS_PER_CLIENT,
    PROTOCOL_VERSION,
    CloseCode,
    PingOp,
    SubscribeOp,
    UnsubscribeOp,
)

log = structlog.get_logger(__name__)
router = APIRouter(tags=["stream"])


@router.post("/ws/ticket")
async def issue_ticket() -> JSONResponse:
    """Exchange an authenticated HTTP request for a 30-second WebSocket ticket.

    Anonymous here because the demo has no user model yet; wire real auth in and
    only the ``subject`` changes. See ``fx_api/ws/tickets.py`` for why this
    endpoint exists rather than a header on the WebSocket itself.
    """
    tickets = get_tickets()
    settings = get_settings()
    token = await tickets.issue("anonymous")
    return JSONResponse({"ticket": token, "expires_in": settings.ws_ticket_ttl_s})


async def _writer(ws: WebSocket, session: ClientSession, deadline_s: float) -> None:
    """Drain the conflating map. The ONLY place that writes to this socket."""
    try:
        while not session.closed:
            # Wake on new data, but time out so saturation is still noticed on a
            # client that has gone completely silent.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(session.wakeup.wait(), timeout=1.0)

            if session.note_saturation(deadline_s):
                SLOW_DISCONNECTS.inc()
                log.warning(
                    "ws.slow_client_dropped",
                    client=session.client_id,
                    conflated=session.conflated,
                )
                await ws.close(CloseCode.SLOW_CLIENT, "client too slow")
                session.closed = True
                return

            for frame in session.drain():
                await ws.send_json(frame)
    except (WebSocketDisconnect, RuntimeError):
        session.closed = True


@router.websocket("/ws/stream")
async def stream(ws: WebSocket, ticket: str = Query(default="")) -> None:
    settings = get_settings()
    manager = get_manager()
    tickets = get_tickets()

    if settings.require_ws_ticket:
        subject = await tickets.redeem(ticket)
        if subject is None:
            await ws.close(CloseCode.UNAUTHORISED, "invalid or expired ticket")
            return

    await ws.accept()
    session = ClientSession(client_id=str(uuid.uuid4()))
    manager.register(session)
    writer = asyncio.create_task(
        _writer(ws, session, settings.ws_slow_client_deadline_s), name="ws-writer"
    )

    try:
        await ws.send_json(
            {
                "type": "hello",
                "protocol": PROTOCOL_VERSION,
                "server_time": datetime.now(UTC).isoformat(),
                "max_symbols": MAX_SYMBOLS_PER_CLIENT,
                "feed": manager.feed_status,
            }
        )

        while True:
            raw = await ws.receive_json()
            op = raw.get("op")

            if op == "subscribe":
                msg = SubscribeOp.model_validate(raw)
                if len(session.symbols | set(msg.symbols)) > MAX_SYMBOLS_PER_CLIENT:
                    await ws.close(CloseCode.TOO_MANY_SYMBOLS, "subscription limit")
                    break
                session.subscribe(msg.symbols)
                # Snapshot THEN delta. The writer task is already running, so any
                # deltas that arrive during this await are queued in `pending`
                # and delivered right after - no gap, and no double-apply,
                # because conflation keeps only the newest value per key anyway.
                snap = await manager.snapshot(msg.symbols, bars=msg.bars)
                await ws.send_json({"type": "snapshot", "data": snap})
                log.info("ws.subscribed", client=session.client_id, symbols=msg.symbols)

            elif op == "unsubscribe":
                session.unsubscribe(UnsubscribeOp.model_validate(raw).symbols)

            elif op == "ping":
                PingOp.model_validate(raw)
                await ws.send_json({"type": "pong", "ts": datetime.now(UTC).isoformat()})

            else:
                await ws.send_json({"type": "error", "message": f"unknown op {op!r}"})

    except WebSocketDisconnect:
        log.info(
            "ws.disconnected",
            client=session.client_id,
            sent=session.sent,
            conflated=session.conflated,
        )
    except pydantic.ValidationError as exc:
        with contextlib.suppress(RuntimeError):
            await ws.send_json({"type": "error", "message": exc.errors()[0]["msg"]})
            await ws.close(CloseCode.PROTOCOL_ERROR, "invalid message")
    finally:
        session.closed = True
        session.wakeup.set()  # let the writer notice and exit
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
        manager.unregister(session.client_id)
