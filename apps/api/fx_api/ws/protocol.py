"""The client<->server WebSocket protocol.

One document, both directions, versioned. Hand-syncing an ad-hoc message shape
between Python and TypeScript is how a live feed quietly starts dropping a field
nobody notices for a week; ``packages/contracts`` generates the TS types from
these models so the two cannot drift.

CLIENT -> SERVER
    {"op": "subscribe",   "symbols": ["EURUSD"], "bars": 240}
    {"op": "unsubscribe", "symbols": ["EURUSD"]}
    {"op": "ping"}

SERVER -> CLIENT
    {"type": "hello",    "protocol": 1, "server_time": "..."}
    {"type": "snapshot", "data": {...}}      <- quote, bars, vol, regime, zhist
    {"type": "tick",     "s": "EURUSD", ...} <- deltas from that point on
    {"type": "vol",      "s": "EURUSD", ...}
    {"type": "status",   "state": "healthy|degraded|fatal|stopped", ...}
    {"type": "error",    "message": "..."}
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

PROTOCOL_VERSION = 1
MAX_SYMBOLS_PER_CLIENT = 25


class SubscribeOp(BaseModel):
    op: Literal["subscribe"]
    symbols: list[str] = Field(min_length=1, max_length=MAX_SYMBOLS_PER_CLIENT)
    bars: int = Field(default=240, ge=0, le=1440)

    @field_validator("symbols")
    @classmethod
    def _normalise(cls, v: list[str]) -> list[str]:
        # Upper-case and de-duplicate, preserving order. Without this, a client
        # sending ["eurusd", "EURUSD"] would occupy two subscription slots and
        # receive every update twice.
        seen: dict[str, None] = {}
        for s in v:
            seen.setdefault(s.strip().upper(), None)
        return list(seen)


class UnsubscribeOp(BaseModel):
    op: Literal["unsubscribe"]
    symbols: list[str] = Field(min_length=1, max_length=MAX_SYMBOLS_PER_CLIENT)


class PingOp(BaseModel):
    op: Literal["ping"]


ClientOp = SubscribeOp | UnsubscribeOp | PingOp


# Close codes. 4000-4999 is the range reserved for application use; using a
# distinct code per reason means the client can react intelligently - reconnect
# immediately after a slow-client drop, but not after a protocol violation.
class CloseCode:
    NORMAL = 1000
    PROTOCOL_ERROR = 4000
    UNAUTHORISED = 4001
    TOO_MANY_SYMBOLS = 4002
    SLOW_CLIENT = 4003
    SERVER_SHUTDOWN = 4004
