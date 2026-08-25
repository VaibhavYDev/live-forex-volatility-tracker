"""Per-client backpressure by conflation.

THE BUG THIS PREVENTS
---------------------
A client on hotel wifi, or a backgrounded tab whose browser has throttled its
timers, cannot drain 50 messages/second. The obvious broadcast loop

    for client in clients:
        await client.ws.send_json(tick)      # <-- do not do this

fails in one of two ways, both fatal:

* the slow client's ``send`` blocks, so ONE bad connection stalls the broadcast
  for EVERY other client; or
* you wrap it in ``create_task`` / an unbounded queue, and memory grows without
  limit until the process is OOM-killed. At 3am. In front of a reviewer.

THE FIX
-------
Exploit a property of the data: **market prices are last-value-wins**. If three
ticks for EURUSD are queued for a slow client, the first two are worthless -
nobody wants to see a price that is already stale. So each client holds

    pending: dict[symbol, payload]      # the LATEST update per symbol, only

Publishing is ``pending[symbol] = payload; wakeup.set()``: O(1), never blocks,
and bounded by the number of *subscribed symbols* rather than by message rate.
A dedicated writer task per client drains the map at whatever speed that client
can manage. Fast clients get everything, slow clients get a decimated but always
*current* view, and nobody blocks anybody.

Conflation is safe here precisely because the data is last-value-wins. It would
be WRONG for an order stream or a trade tape, where every event is semantically
required. Knowing which kind of stream you have is the actual engineering.

Then instrument it: ``frames_conflated_total`` on the Grafana dashboard turns
"we handle backpressure" from a claim into a graph.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from prometheus_client import Counter, Gauge

log = structlog.get_logger(__name__)

FRAMES_SENT = Counter("fx_ws_frames_sent_total", "Frames written to clients")
FRAMES_CONFLATED = Counter(
    "fx_ws_frames_conflated_total",
    "Updates superseded before the client could read them (backpressure working)",
)
CLIENTS = Gauge("fx_ws_clients_connected", "Live WebSocket clients")
SLOW_DISCONNECTS = Counter("fx_ws_slow_client_disconnects_total", "Clients dropped as too slow")


@dataclass(slots=True)
class ClientSession:
    """One browser connection.

    ``pending`` is keyed by a routing key (``tick:EURUSD``, ``vol:EURUSD``,
    ``status``) rather than by symbol alone, so a volatility update never
    silently overwrites a price update for the same pair.
    """

    client_id: str
    symbols: set[str] = field(default_factory=set)
    pending: dict[str, Any] = field(default_factory=dict)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    conflated: int = 0
    sent: int = 0
    closed: bool = False
    _saturated_since: float | None = None

    def offer(self, key: str, payload: Any) -> None:
        """Non-blocking publish. This is the hot path; it must never await."""
        if self.closed:
            return
        if key in self.pending:
            # The previous value for this key was never read. Dropping it is the
            # point: the client is about to receive something strictly newer.
            self.conflated += 1
            FRAMES_CONFLATED.inc()
        self.pending[key] = payload
        self.wakeup.set()

    def drain(self) -> list[Any]:
        """Take everything queued. Called only by this client's writer task."""
        if not self.pending:
            return []
        batch = list(self.pending.values())
        self.pending.clear()
        self.wakeup.clear()
        self.sent += len(batch)
        FRAMES_SENT.inc(len(batch))
        return batch

    def note_saturation(self, deadline_s: float) -> bool:
        """True when this client has been behind for longer than the deadline.

        A client that conflates occasionally is fine - that is the mechanism
        working. A client whose queue is *continuously* non-empty is not reading
        at all (laptop asleep, connection black-holed) and is holding memory for
        nothing. Disconnect it with an honest close code and let it reconnect.
        """
        if not self.pending:
            self._saturated_since = None
            return False
        now = time.monotonic()
        if self._saturated_since is None:
            self._saturated_since = now
            return False
        return (now - self._saturated_since) > deadline_s

    def subscribe(self, symbols: list[str]) -> set[str]:
        self.symbols.update(s.upper() for s in symbols)
        return self.symbols

    def unsubscribe(self, symbols: list[str]) -> set[str]:
        for s in symbols:
            self.symbols.discard(s.upper())
            # Drop queued updates for a symbol the client no longer wants, so an
            # unsubscribe takes effect immediately rather than after a flush.
            self.pending.pop(f"tick:{s.upper()}", None)
            self.pending.pop(f"vol:{s.upper()}", None)
        return self.symbols

    def wants(self, symbol: str) -> bool:
        return symbol.upper() in self.symbols
