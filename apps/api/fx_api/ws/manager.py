"""Cross-replica fan-out.

THE PROBLEM
-----------
The browser connects to API replica B. The ingestor runs next to replica A.
Replica B has no idea a tick happened.

THE FIX
-------
The ingestor PUBLISHes to ``ch:tick:{SYMBOL}``; **each API replica holds exactly
one Redis subscription** and routes locally to its own interested clients.

Note the cardinality, because it is the whole point: one Redis subscriber per
*process*, not per client. Five hundred browsers across three replicas means
three Redis subscriptions. The naive design - a subscription per browser - would
put 500 connections on Redis and fall over well before that.

WHY PUB/SUB HERE AND STREAMS FOR THE PERSISTER
----------------------------------------------
They solve different problems, and using each for its strength is the decision:

                     Redis Streams            Redis Pub/Sub
    delivery         at-least-once, acked     fire-and-forget
    consumer down    waits in the PEL         gone
    cost per browser a group + polling        free (process-level)
    used for         DURABILITY -> Postgres   FAN-OUT -> browsers

Persistence must never lose a tick, so it reads the Stream. A browser that missed
40ms of ticks genuinely does not care - and would rather have the newest price
than a replay of an old one - so it gets Pub/Sub.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import redis.asyncio as aioredis
import structlog
from fx_core import keys

from fx_api.ws.conflator import CLIENTS, ClientSession

log = structlog.get_logger(__name__)


class ConnectionManager:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._clients: dict[str, ClientSession] = {}
        self._pubsub: aioredis.client.PubSub | None = None
        self._task: asyncio.Task[None] | None = None
        self.feed_status: dict[str, Any] = {"state": "unknown", "ts": None}

    # ------------------------------------------------------------- lifecycle
    async def refresh_feed_status(self) -> dict[str, Any]:
        """Read the stored feed status.

        Covers the late-joiner case that Pub/Sub structurally cannot: a replica
        that starts after the last status change has no message to have missed.
        An absent key means the ingestor's heartbeat has expired - which is
        itself the answer, and a more trustworthy one than a stale cached value.
        """
        raw = await self._redis.get(keys.FEED_STATUS)
        self.feed_status = (
            json.loads(raw) if raw else {"state": "unknown", "detail": "no ingestor heartbeat"}
        )
        return self.feed_status

    async def start(self) -> None:
        with contextlib.suppress(Exception):
            await self.refresh_feed_status()
        self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        await self._pubsub.psubscribe(
            keys.channel_tick("*"),
            keys.channel_vol("*"),
            keys.channel_alert("*"),
            keys.channel_status(),
        )
        self._task = asyncio.create_task(self._router(), name="pubsub-router")
        log.info("fanout.started")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._pubsub is not None:
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]  # redis-py is unannotated here
            self._pubsub = None
        for session in list(self._clients.values()):
            session.closed = True
            session.wakeup.set()
        log.info("fanout.stopped")

    # ------------------------------------------------------------- registry
    def register(self, session: ClientSession) -> None:
        self._clients[session.client_id] = session
        CLIENTS.set(len(self._clients))

    def unregister(self, client_id: str) -> None:
        self._clients.pop(client_id, None)
        CLIENTS.set(len(self._clients))

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # --------------------------------------------------------------- routing
    async def _router(self) -> None:
        assert self._pubsub is not None
        async for message in self._pubsub.listen():
            if message is None or message.get("type") not in ("message", "pmessage"):
                continue
            try:
                self._dispatch(str(message["channel"]), str(message["data"]))
            except Exception:
                log.exception("fanout.dispatch_failed", channel=message.get("channel"))

    def _dispatch(self, channel: str, raw: str) -> None:
        payload = json.loads(raw)

        if channel == keys.channel_status():
            # Feed health goes to EVERY client regardless of subscription. A user
            # watching one pair still needs to know the whole feed is degraded.
            self.feed_status = payload
            frame = {"type": "status", **payload}
            for session in self._clients.values():
                session.offer("status", frame)
            return

        symbol = channel.rsplit(":", 1)[-1]
        if channel.startswith("ch:tick"):
            kind, key = "tick", f"tick:{symbol}"
        elif channel.startswith("ch:vol"):
            kind, key = "vol", f"vol:{symbol}"
        else:
            # ALERTS MUST NOT BE CONFLATED.
            #
            # Conflation is safe for prices because they are last-value-wins: a
            # superseded tick is a stale price nobody wants. A regime transition
            # is a discrete EVENT - "we escalated at 14:03" is not made redundant
            # by "we cleared at 14:31", and a client that only ever saw the clear
            # would have no idea anything happened.
            #
            # This is exactly the caveat in docs/adr/0006: conflation is a
            # property of the data, not a general-purpose mechanism. Keying by
            # seq gives every transition its own slot, so none is dropped.
            # Memory stays bounded because transitions are rare, and a client
            # that never drains is disconnected by the saturation deadline.
            kind = "alert"
            key = f"alert:{symbol}:{payload.get('seq', 0)}"

        frame = {"type": kind, **payload}

        # Server-side subscription filtering: a client watching 3 pairs receives
        # 3 pairs. Obvious, and routinely skipped - most implementations send
        # everything and filter in the browser, wasting the user's bandwidth.
        for session in self._clients.values():
            if session.wants(symbol):
                session.offer(key, frame)

    # -------------------------------------------------------------- snapshot
    async def snapshot(self, symbols: list[str], bars: int = 240) -> dict[str, Any]:
        """Snapshot-then-delta, served entirely from Redis.

        The standard market-data pattern: give the client a consistent picture of
        "now", then stream changes. Crucially this reads ``q:*`` and ``hist:*``
        from Redis and NEVER touches Postgres, so a burst of reconnects (a deploy,
        a wifi blip across an office) cannot turn into a thundering herd of
        database queries at the exact moment the system is already stressed.
        """
        fields = 5
        pipe = self._redis.pipeline(transaction=False)
        for symbol in symbols:
            pipe.hgetall(keys.quote(symbol))
            pipe.zrevrange(keys.history(symbol), 0, bars - 1)
            pipe.hgetall(keys.vol_state(symbol, "1h"))
            pipe.get(keys.regime_state(symbol))
            # Same window as the bars, because they are the same minutes. Asking
            # for a different count would leave the price chart and the z pane
            # showing different spans of time on the same screen.
            pipe.zrevrange(keys.zhist(symbol), 0, bars - 1)
        results = await pipe.execute()

        out: dict[str, Any] = {}
        for i, symbol in enumerate(symbols):
            base = i * fields
            quote, history, vol, regime, zhist = results[base : base + fields]
            out[symbol] = {
                "quote": {k: _maybe_float(v) for k, v in (quote or {}).items()},
                "bars": [json.loads(b) for b in reversed(history or [])],
                "vol": {k: json.loads(v) for k, v in (vol or {}).items()},
                # The z series the pane draws on first paint. Without it a
                # browser opening mid-session watches an empty chart fill in one
                # minute at a time, which reads as broken rather than new.
                "zhist": [json.loads(z) for z in reversed(zhist or [])],
                # Current regime, so a browser connecting MID-EVENT shows
                # "stressed" immediately instead of looking calm until the next
                # transition happens to fire. Pub/Sub cannot answer "what did I
                # miss"; this is the snapshot half of snapshot-then-delta applied
                # to alerting.
                "regime": _regime_from_snapshot(regime),
            }
        return out


def _maybe_float(value: str) -> float | str:
    try:
        return float(value)
    except ValueError:
        return value


def _regime_from_snapshot(raw: str | None) -> dict[str, Any]:
    """Pull just the committed regime out of the detector snapshot.

    The snapshot also carries the baseline and the diurnal profile, which a
    browser has no use for - sending 24 hourly factors to every client on every
    subscribe would be pure waste. A missing key means the ingestor has not
    sealed a bar for this symbol yet, which is honestly reported as "unknown"
    rather than optimistically as "normal".
    """
    if not raw:
        return {"regime": "unknown", "seq": 0}
    try:
        trigger = json.loads(raw)["trigger"]
        return {
            "regime": trigger["regime"],
            "seq": trigger["seq"],
            "since": trigger.get("regime_since"),
        }
    except (ValueError, KeyError, TypeError):
        return {"regime": "unknown", "seq": 0}
