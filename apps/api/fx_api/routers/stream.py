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
from typing import Any

import pydantic
import structlog
from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from prometheus_client import Counter

from fx_api.deps import get_manager, get_redis, get_settings, get_tickets
from fx_api.ratelimit import client_id, hit
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

WS_REFUSED_FULL = Counter("fx_ws_refused_full_total", "Sockets refused at the client cap")


@router.post("/ws/ticket")
async def issue_ticket(request: Request) -> JSONResponse:
    """Exchange an authenticated HTTP request for a 30-second WebSocket ticket.

    Anonymous here because the demo has no user model yet; wire real auth in and
    only the ``subject`` changes. See ``fx_api/ws/tickets.py`` for why this
    endpoint exists rather than a header on the WebSocket itself.

    Rate limited because it writes to Redis and is reachable without credentials:
    the limit bounds what an unauthenticated caller can cost us. It does not make
    the feed private, and SECURITY.md says so rather than implying otherwise.
    """
    tickets = get_tickets()
    settings = get_settings()

    verdict = await hit(
        get_redis(), "ticket", client_id(request), settings.ticket_limit_per_min, 60
    )
    if not verdict.allowed:
        return JSONResponse(
            {"detail": f"rate limit is {settings.ticket_limit_per_min}/min"},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(verdict.retry_after_s)},
        )

    token = await tickets.issue("anonymous")
    return JSONResponse({"ticket": token, "expires_in": settings.ws_ticket_ttl_s})


async def _writer(
    ws: WebSocket, session: ClientSession, deadline_s: float, send_lock: asyncio.Lock
) -> None:
    """Drain the conflating map. The ONLY place that writes to this socket."""
    try:
        while not session.closed:
            # Wake on new data, but time out so saturation is still noticed on a
            # client that has gone completely silent.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(session.wakeup.wait(), timeout=1.0)

            if session.note_saturation(deadline_s):
                await _drop_slow_client(ws, session, "backlog")
                return

            async with send_lock:
                batch = session.drain()
                try:
                    # A client whose socket stops accepting data blocks this
                    # send, and a writer stuck in it never reaches the check
                    # above. The same deadline bounds the send itself.
                    await asyncio.wait_for(_send_all(ws, batch), timeout=deadline_s)
                except TimeoutError:
                    await _drop_slow_client(ws, session, "send")
                    return
    except (WebSocketDisconnect, RuntimeError):
        session.closed = True


async def _send_all(ws: WebSocket, frames: list[Any]) -> None:
    for frame in frames:
        await ws.send_json(frame)


async def _drop_slow_client(ws: WebSocket, session: ClientSession, cause: str) -> None:
    SLOW_DISCONNECTS.inc()
    log.warning(
        "ws.slow_client_dropped",
        client=session.client_id,
        conflated=session.conflated,
        cause=cause,
    )
    session.closed = True
    # Bounded: the close frame goes down the same socket that stopped taking data.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(ws.close(CloseCode.SLOW_CLIENT, "client too slow"), timeout=1.0)


async def _admit(ws: WebSocket, ticket: str) -> bool:
    """Close the socket before accepting it when it may not join; True if it may.

    The cap comes first, so a refused client keeps its ticket for the retry. One
    socket past the cap is refused (1013, try again later) rather than slowing
    every client already admitted.
    """
    settings = get_settings()
    if get_manager().client_count >= settings.ws_max_clients:
        WS_REFUSED_FULL.inc()
        await ws.close(CloseCode.TRY_AGAIN_LATER, "server at capacity")
        return False
    if settings.require_ws_ticket and await get_tickets().redeem(ticket) is None:
        await ws.close(CloseCode.UNAUTHORISED, "invalid or expired ticket")
        return False
    return True


@router.websocket("/ws/stream")
async def stream(ws: WebSocket, ticket: str = Query(default="")) -> None:
    settings = get_settings()
    manager = get_manager()

    if not await _admit(ws, ticket):
        return

    await ws.accept()
    session = ClientSession(client_id=str(uuid.uuid4()))
    manager.register(session)
    # Held across the hello and each snapshot: a delta sent ahead of its snapshot
    # would be overwritten in the browser by the older snapshot.
    send_lock = asyncio.Lock()
    writer = asyncio.create_task(
        _writer(ws, session, settings.ws_slow_client_deadline_s, send_lock), name="ws-writer"
    )

    try:
        async with send_lock:
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
                # Snapshot THEN delta. Deltas that arrive during this await are
                # queued in `pending` and delivered right after - no gap, and no
                # double-apply, because conflation keeps only the newest value per
                # key anyway.
                async with send_lock:
                    session.subscribe(msg.symbols)
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
