"""PostgreSQL access for the persister.

THE IDEMPOTENCY STORY (the important part of this file)
------------------------------------------------------
Redis Streams consumer groups give **at-least-once** delivery. If the worker
commits to Postgres and dies before ``XACK``, the whole batch is redelivered.
That is not a bug to be avoided - it is the guarantee, and the correct response
is to make redelivery *harmless* rather than to chase exactly-once, which does
not exist across two systems.

A plain ``ON CONFLICT DO NOTHING`` would be enough if bars were immutable. They
are not: a minute's bar is built up across several batches, so the upsert has to
*merge* - and merging additive columns (``tick_count``, ``sum_ret``) is NOT
idempotent. Redeliver a batch and you double-count it.

The fix is a **row-level watermark**. Every bar stores the highest Redis stream
id that has contributed to it, and the merge only applies when the incoming batch
carries a strictly higher id:

    WHERE EXCLUDED.last_stream_id > bars_1m.last_stream_id

Stream ids are monotonic, and a batch is one transaction, so a redelivered batch
carries ids at or below the stored watermark and is skipped wholesale. That turns
at-least-once delivery into an exactly-once *effect* - which is the only kind
available. It is the same fencing-token idea as the leader lease, applied per row.

KNOWN, BOUNDED, ACCEPTED
------------------------
The watermark does not deduplicate the same *tick* arriving under two different
stream ids, which is what a brief double-leader window (a GC pause longer than
the lease TTL) produces. Worst case: one minute's ``tick_count`` is inflated for
one bar. OHLC is unaffected - max, min and last-write-wins are all idempotent -
and the variance error is second-order. We accept it and say so in
``docs/adr/0005``, rather than adding a distributed transaction to fix a
sub-10-second edge case that degrades one number slightly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TypedDict, cast

import asyncpg
import structlog
from fx_core.models import Bar

log = structlog.get_logger(__name__)

_UPSERT = """
INSERT INTO bars_1m (
    symbol, bucket, open, high, low, close,
    tick_count, sum_ret, sum_ret_sq, source, last_stream_id
)
SELECT * FROM unnest(
    $1::text[], $2::timestamptz[], $3::double precision[], $4::double precision[],
    $5::double precision[], $6::double precision[], $7::integer[],
    $8::double precision[], $9::double precision[], $10::bar_source[], $11::text[]
)
ON CONFLICT (symbol, bucket) DO UPDATE SET
    high           = GREATEST(bars_1m.high, EXCLUDED.high),
    low            = LEAST(bars_1m.low, EXCLUDED.low),
    close          = EXCLUDED.close,
    tick_count     = bars_1m.tick_count + EXCLUDED.tick_count,
    sum_ret        = bars_1m.sum_ret + EXCLUDED.sum_ret,
    sum_ret_sq     = bars_1m.sum_ret_sq + EXCLUDED.sum_ret_sq,
    last_stream_id = EXCLUDED.last_stream_id,
    -- Backfilled data never silently overwrites streamed data's provenance.
    source         = CASE WHEN bars_1m.source = 'stream' THEN bars_1m.source
                          ELSE EXCLUDED.source END
WHERE EXCLUDED.last_stream_id > bars_1m.last_stream_id
"""


class AlertRow(TypedDict):
    """One regime transition, ready for insert. Mirrors alert_events exactly."""

    symbol: str
    seq: int
    ts: datetime
    old_regime: str
    new_regime: str
    trigger_value: float | None
    threshold_value: float
    sigma: float | None
    cause: str
    reason: str


# ON CONFLICT DO NOTHING makes a redelivered batch a no-op; RETURNING tells us
# which rows actually landed, so the caller can tell "already stored" from
# "silently dropped". Without the RETURNING clause a seq collision after state
# loss would be indistinguishable from success.
_INSERT_ALERTS = """
INSERT INTO alert_events (
    symbol, seq, ts, old_regime, new_regime,
    trigger_value, threshold_value, sigma, cause, reason
)
SELECT * FROM unnest(
    $1::text[], $2::bigint[], $3::timestamptz[], $4::regime[], $5::regime[],
    $6::double precision[], $7::double precision[], $8::double precision[],
    $9::transition_cause[], $10::text[]
)
ON CONFLICT (symbol, seq) DO NOTHING
RETURNING symbol, seq
"""


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self, min_size: int = 2, max_size: int = 10) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=min_size, max_size=max_size, command_timeout=30.0
        )
        log.info("db.connected", min_size=min_size, max_size=max_size)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() has not been called")
        return self._pool

    async def healthy(self) -> bool:
        try:
            async with self.pool.acquire() as conn:
                return bool(await conn.fetchval("SELECT 1"))
        except (asyncpg.PostgresError, OSError):
            return False

    async def upsert_bars(self, bars: Sequence[tuple[Bar, str]]) -> int:
        """Merge a batch of (bar, last_stream_id) pairs in ONE transaction.

        One statement with array parameters rather than executemany: a single
        round trip and a single plan, instead of N of each.
        """
        if not bars:
            return 0

        cols: list[list[object]] = [[] for _ in range(11)]
        for bar, stream_id in bars:
            values = (
                bar.symbol,
                bar.bucket,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.tick_count,
                bar.sum_ret,
                bar.sum_ret_sq,
                str(bar.source),
                stream_id,
            )
            for i, v in enumerate(values):
                cols[i].append(v)

        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(_UPSERT, *cols)
        return len(bars)

    async def fetch_bars(
        self, symbol: str, limit: int = 500, interval: str = "1m"
    ) -> list[asyncpg.Record]:
        table = {"1m": "bars_1m", "5m": "bars_5m", "1h": "bars_1h"}.get(interval, "bars_1m")
        async with self.pool.acquire() as conn:
            return list(
                await conn.fetch(
                    f"SELECT bucket, open, high, low, close, tick_count, source "
                    f"FROM {table} WHERE symbol = $1 ORDER BY bucket DESC LIMIT $2",
                    symbol,
                    limit,
                )
            )

    async def insert_alerts(self, rows: Sequence[AlertRow]) -> tuple[int, list[tuple[str, int]]]:
        """Insert regime transitions idempotently.

        Returns ``(inserted, conflicted)`` where ``conflicted`` lists the
        ``(symbol, seq)`` pairs that already existed. The caller decides what a
        conflict means - see ``fx_worker.alerts`` - because there are two very
        different reasons for one and only the caller can tell them apart:

        * a redelivered batch, which is the guarantee working as designed;
        * a reused ``seq`` after the Redis detector snapshot was lost, which
          would otherwise SILENTLY DISCARD a real alert.

        Returning the conflicts rather than swallowing them is what makes the
        second case detectable at all.
        """
        if not rows:
            return 0, []

        cols: list[list[object]] = [[] for _ in range(10)]
        for row in rows:
            values = (
                row["symbol"],
                row["seq"],
                row["ts"],
                row["old_regime"],
                row["new_regime"],
                row["trigger_value"],
                row["threshold_value"],
                row["sigma"],
                row["cause"],
                row["reason"],
            )
            for i, value in enumerate(values):
                cols[i].append(value)

        async with self.pool.acquire() as conn, conn.transaction():
            inserted = await conn.fetch(_INSERT_ALERTS, *cols)

        landed = {(r["symbol"], r["seq"]) for r in inserted}
        conflicted = [
            (row["symbol"], row["seq"]) for row in rows if (row["symbol"], row["seq"]) not in landed
        ]
        return len(landed), conflicted

    async def alert_timestamp(self, symbol: str, seq: int) -> datetime | None:
        """The ts already stored for this (symbol, seq). Used to classify a conflict."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT ts FROM alert_events WHERE symbol = $1 AND seq = $2", symbol, seq
            )
        return cast("datetime | None", row)

    async def current_regimes(self) -> dict[str, str]:
        """Latest committed regime per symbol, from the ``regime_current`` view."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT symbol, regime FROM regime_current")
        return {r["symbol"]: r["regime"] for r in rows}

    async def last_bucket(self, symbol: str) -> datetime | None:
        """Newest persisted bucket - the anchor for gap detection on reconnect."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchval("SELECT max(bucket) FROM bars_1m WHERE symbol = $1", symbol)
        return cast("datetime | None", row)
