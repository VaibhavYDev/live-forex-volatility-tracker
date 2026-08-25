"""A WebSocket server that misbehaves on demand.

You cannot ask Tiingo to drop your connection mid-frame, send you malformed JSON,
or go silent for sixty seconds. So we run a server that will do all of those on
command, and assert the invariants that the README claims.

This is the highest-leverage directory in the repository. Every fault-tolerance
claim on the front page has a test here that proves it, and no other forex
tracker on GitHub has one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import websockets
from websockets.asyncio.server import Server, ServerConnection


@dataclass
class FaultConfig:
    """What the server should do wrong."""

    # Close the connection abruptly after N frames (simulates a network drop).
    drop_after_frames: int | None = None
    # Emit an unparseable frame every N frames.
    malformed_every: int | None = None
    # Stop sending anything after N frames, but hold the socket open. This is the
    # nastiest failure: TCP stays healthy, the app sees only silence, and it is
    # indistinguishable from a quiet market without a watchdog.
    stall_after_frames: int | None = None
    # Re-send the same tick, to prove downstream idempotency.
    duplicate_every: int | None = None
    # Emit an out-of-order timestamp every N frames.
    out_of_order_every: int | None = None
    # Reject the subscription with an auth error.
    reject_auth: bool = False
    frames_per_second: float = 200.0


@dataclass
class FaultServer:
    config: FaultConfig = field(default_factory=FaultConfig)
    frames_sent: int = 0
    connections: int = 0
    _server: Server | None = None
    _port: int = 0
    _stopping: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}"

    async def _handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        cfg = self.config

        with contextlib.suppress(TimeoutError, websockets.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), timeout=2.0)  # the subscribe frame

        if cfg.reject_auth:
            await ws.send(
                json.dumps({"messageType": "E", "response": {"message": "invalid token"}})
            )
            await ws.close()
            return

        price = 1.0842
        sent_this_connection = 0

        try:
            while True:
                if (
                    cfg.stall_after_frames is not None
                    and sent_this_connection >= cfg.stall_after_frames
                ):
                    # Hold the socket open and say nothing. Only an application
                    # level watchdog can detect this. Poll a stop flag rather
                    # than sleeping forever, so teardown does not hang: a stalled
                    # handler is doing no I/O, so closing the server cannot
                    # interrupt it.
                    await self._stopping.wait()
                    return

                if (
                    cfg.drop_after_frames is not None
                    and sent_this_connection >= cfg.drop_after_frames
                ):
                    await ws.close(code=1006)
                    return

                sent_this_connection += 1
                self.frames_sent += 1
                n = self.frames_sent

                if cfg.malformed_every and n % cfg.malformed_every == 0:
                    await ws.send("{not json at all")
                    await asyncio.sleep(1.0 / cfg.frames_per_second)
                    continue

                price *= 1.0 + ((n % 7) - 3) * 1e-5
                ts = datetime.now(UTC)
                if cfg.out_of_order_every and n % cfg.out_of_order_every == 0:
                    ts = ts.replace(year=ts.year - 1)

                frame = json.dumps(
                    {
                        "service": "fx",
                        "messageType": "A",
                        "data": [
                            "Q",
                            "eurusd",
                            ts.isoformat().replace("+00:00", "Z"),
                            1_000_000,
                            round(price - 0.00005, 6),
                            round(price, 6),
                            round(price + 0.00005, 6),
                            1_000_000,
                        ],
                    }
                )
                await ws.send(frame)
                if cfg.duplicate_every and n % cfg.duplicate_every == 0:
                    await ws.send(frame)

                await asyncio.sleep(1.0 / cfg.frames_per_second)
        except websockets.ConnectionClosed:
            return

    async def __aenter__(self) -> FaultServer:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self._port = next(iter(self._server.sockets)).getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@contextlib.asynccontextmanager
async def fault_server(config: FaultConfig) -> AsyncIterator[FaultServer]:
    async with FaultServer(config=config) as server:
        yield server
