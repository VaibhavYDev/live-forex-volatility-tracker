"""Recover the minutes the feed missed, from the provider's own one-minute history.

WHY
---
A minute with no tick has no row in ``bars_1m``. On 2026-10-12 Windows Modern
Standby stopped the stack from 11:42 to 14:35 and sleep stopped it again at
16:46; nothing was stored until 2026-10-17, while the provider kept every one
of those minutes. Nothing ever asked for them: minute history was only loaded
into an empty Redis cache, and never into PostgreSQL.

WHAT A RUN DOES, AND WILL NOT DO
--------------------------------
* It looks only at open-market minutes (``fx_core.calendar``, New York 17:00
  boundaries, so daylight saving is handled) with no row in ``bars_1m``.
* It looks only at minutes older than the tick stream's retention plus two
  minutes. Until then the worker may still persist live ticks for that minute,
  and its merge would fold provider prices into a live bar.
* It stores only what the provider returns. A minute the provider has no bar
  for stays missing and is recorded as silent: nothing is interpolated.
* It inserts if absent (``ON CONFLICT DO NOTHING``): a stored bar - live, or
  from an earlier run - is never changed.
* Recovered rows carry ``source = 'backfill'``, ``tick_count = 0``, zero return
  sums and the run id. The provider gives OHLC only; no tick count or tick-level
  variance is known, and none is implied.
* It never writes the tick stream, Pub/Sub channels, quotes, the regime
  detector, the z-score series or alerts. Recovered history is not a market
  event, and nothing downstream may mistake it for one.

ORDER OF WRITES, AND WHAT A CRASH LEAVES BEHIND
-----------------------------------------------
1. ``bars_1m``, one transaction per provider request;
2. the Redis hot cache: only minutes from the last 24 hours (the cache's span),
   only if absent, never past its 1,440-bar bound;
3. ``bars_5m`` / ``bars_1h`` refreshed over what changed, and over closed buckets
   the refresh policies left stale (they only look back an hour or a day);
4. the run record.

Every write is idempotent, so a crash between steps leaves valid rows and the
next run repairs the rest: it marks the dead run ``aborted`` (holding the
advisory lock proves no other run is alive), re-detects what is still missing,
refreshes stale rollups and adds recovered rows the cache lacks - without
asking the provider again for minutes it already stored.

Runs cannot overlap: each holds a PostgreSQL advisory lock for its whole
duration, released by the server if the process dies.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import asyncpg
import redis.asyncio as aioredis
import structlog
from fx_core import keys
from fx_core.calendar import is_market_open
from fx_core.models import Bar, BarSource
from prometheus_client import Counter

from fx_ingestor.config import IngestorSettings
from fx_ingestor.providers import (
    MarketDataProvider,
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderError,
    ProviderRateLimitError,
    build_provider,
)
from fx_ingestor.quota import RequestQuota

log = structlog.get_logger(__name__)

MINUTE: Final = timedelta(minutes=1)
# The live writer keeps the newest 1,440 one-minute bars (pipeline._write).
HOT_CACHE_BARS: Final = 1_440
# A span fetched this long after its last minute is settled: what the provider
# did not have by then, later runs do not ask for again. Until then it is asked
# for at most once in each such interval, not on every run.
SETTLE: Final = timedelta(hours=1)
# How long to leave the provider alone after it refused us or asked us to slow down.
AUTH_BACKOFF: Final = timedelta(hours=6)
RATE_LIMIT_BACKOFF: Final = timedelta(minutes=15)
# pg_advisory_lock key for "a recovery run is in progress" (the bytes of "fxrecovr").
LOCK_KEY: Final = 0x6678_7265_636F_7672

# (view, bucket width, its refresh policy's end_offset): a bucket is refreshed
# only once it is closed and past the offset, exactly as the policy would.
ROLLUPS: Final = (
    ("bars_5m", timedelta(minutes=5), timedelta(minutes=1)),
    ("bars_1h", timedelta(hours=1), timedelta(minutes=5)),
)

RUNS = Counter("fx_recovery_runs_total", "History recovery runs by outcome", ["status"])
BARS_RECOVERED = Counter(
    "fx_recovery_bars_inserted_total", "Minutes added to bars_1m from provider history", ["symbol"]
)


# ---------------------------------------------------------------- planning --
def floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def floor_bucket(ts: datetime, width: timedelta) -> datetime:
    """time_bucket() for widths that divide a day, which all of ours do."""
    seconds = int(width.total_seconds())
    return datetime.fromtimestamp(int(ts.timestamp()) // seconds * seconds, tz=UTC)


@dataclass(frozen=True, slots=True)
class Window:
    """Bar-start minutes ``start`` through ``end`` inclusive."""

    start: datetime
    end: datetime


def recovery_window(now: datetime, lookback: timedelta, horizon: timedelta) -> Window | None:
    """The minutes a run may look at: no older than ``lookback``, none newer than ``horizon``.

    A minute is eligible once its bar ended more than ``horizon`` ago.
    """
    end = floor_minute(now - horizon) - MINUTE
    start = floor_minute(now - lookback)
    return Window(start, end) if start <= end else None


def open_minutes(
    window: Window, is_open: Callable[[datetime], bool] = is_market_open
) -> list[datetime]:
    minutes: list[datetime] = []
    m = window.start
    while m <= window.end:
        if is_open(m):
            minutes.append(m)
        m += MINUTE
    return minutes


@dataclass(frozen=True, slots=True)
class Span:
    """A run of consecutive missing minutes for one symbol (bar starts, inclusive)."""

    symbol: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start) / MINUTE) + 1

    def each_minute(self) -> Iterable[datetime]:
        m = self.start
        while m <= self.end:
            yield m
            m += MINUTE


def missing_spans(
    symbol: str,
    expected: Sequence[datetime],
    present: set[datetime],
    settled: Sequence[tuple[datetime, datetime]] = (),
) -> list[Span]:
    """Expected minutes with no stored bar, grouped into runs of consecutive minutes.

    A closure breaks a run: the minutes either side of a weekend are not
    consecutive, so a Friday gap and a Sunday gap are two spans, never one.
    """
    spans: list[Span] = []
    start: datetime | None = None
    prev: datetime | None = None
    for m in expected:
        missing = m not in present and not any(a <= m <= b for a, b in settled)
        if missing and start is not None and prev is not None and m == prev + MINUTE:
            prev = m
            continue
        if start is not None and prev is not None:
            spans.append(Span(symbol, start, prev))
            start = prev = None
        if missing:
            start = prev = m
    if start is not None and prev is not None:
        spans.append(Span(symbol, start, prev))
    return spans


@dataclass(frozen=True, slots=True)
class Request:
    """One history request: the spans of one symbol within a few UTC days."""

    symbol: str
    spans: tuple[Span, ...]

    @property
    def start(self) -> datetime:
        return self.spans[0].start

    @property
    def end(self) -> datetime:
        return self.spans[-1].end

    @property
    def minutes(self) -> int:
        return sum(s.minutes for s in self.spans)


def _split_by_days(span: Span, days: int) -> list[Span]:
    parts: list[Span] = []
    start = span.start
    while start <= span.end:
        boundary = datetime.combine(start.date(), datetime.min.time(), UTC) + timedelta(days=days)
        end = min(span.end, boundary - MINUTE)
        parts.append(Span(span.symbol, start, end))
        start = end + MINUTE
    return parts


def plan_requests(spans: Sequence[Span], days_per_request: int) -> list[Request]:
    """Group spans into provider requests of at most ``days_per_request`` UTC calendar days.

    The provider takes whole days (startDate / endDate), so one request covers
    every span in those days; a span longer than the limit is split at a day
    boundary. Requests come out symbol by symbol, oldest first.
    """
    requests: list[Request] = []
    for symbol in sorted({s.symbol for s in spans}):
        current: list[Span] = []
        for whole in sorted((s for s in spans if s.symbol == symbol), key=lambda s: s.start):
            for part in _split_by_days(whole, days_per_request):
                if current and (part.end.date() - current[0].start.date()).days >= days_per_request:
                    requests.append(Request(symbol, tuple(current)))
                    current = []
                current.append(part)
        if current:
            requests.append(Request(symbol, tuple(current)))
    return requests


def refresh_range(
    first: datetime, last: datetime, width: timedelta, end_offset: timedelta, now: datetime
) -> tuple[datetime, datetime] | None:
    """The refresh window covering buckets ``first``..``last``, stopping short of open buckets."""
    start = floor_bucket(first, width)
    end = min(floor_bucket(last, width) + width, floor_bucket(now - end_offset, width))
    return (start, end) if start < end else None


# ------------------------------------------------------------------ report --
@dataclass(slots=True)
class SpanResult:
    span: Span
    status: str
    received: int = 0
    inserted: int = 0
    present: int = 0
    silent: int = 0
    error: str | None = None


@dataclass(slots=True)
class RunReport:
    status: str  # idle | skipped | dry_run | succeeded | partial | failed | aborted
    reason: str = ""
    run_id: uuid.UUID | None = None
    window: Window | None = None
    minutes_missing: int = 0
    requests_planned: int = 0
    requests: int = 0
    bars_received: int = 0
    bars_inserted: int = 0
    bars_present: int = 0
    minutes_silent: int = 0
    cache_added: int = 0
    rollups_refreshed: list[tuple[str, datetime, datetime]] = field(default_factory=list)
    stale_runs_aborted: int = 0
    spans: list[SpanResult] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "run_id": str(self.run_id) if self.run_id else None,
            "window": [self.window.start.isoformat(), self.window.end.isoformat()]
            if self.window
            else None,
            "minutes_missing": self.minutes_missing,
            "requests_planned": self.requests_planned,
            "requests": self.requests,
            "bars_received": self.bars_received,
            "bars_inserted": self.bars_inserted,
            "bars_present": self.bars_present,
            "minutes_silent": self.minutes_silent,
            "cache_added": self.cache_added,
            "rollups_refreshed": [
                (v, a.isoformat(), b.isoformat()) for v, a, b in self.rollups_refreshed
            ],
            "stale_runs_aborted": self.stale_runs_aborted,
        }


# ------------------------------------------------------------------- store --
_INSERT_BARS = """
INSERT INTO bars_1m (
    symbol, bucket, open, high, low, close,
    tick_count, sum_ret, sum_ret_sq, source, last_stream_id, recovery_run_id
)
SELECT $1, t.bucket, t.open, t.high, t.low, t.close, 0, 0, 0, 'backfill', '0-0', $2
FROM unnest(
    $3::timestamptz[], $4::double precision[], $5::double precision[],
    $6::double precision[], $7::double precision[]
) AS t(bucket, open, high, low, close)
ON CONFLICT (symbol, bucket) DO NOTHING
RETURNING bucket
"""

# Stale = a closed bucket whose stored rollup differs from a fold of bars_1m, or
# that exists on one side only. Compared on OHLC and the tick count.
_STALE_ROLLUP = """
WITH fold AS (
    SELECT symbol, time_bucket($3::interval, bucket) AS b,
           first(open, bucket) AS o, max(high) AS h, min(low) AS l,
           last(close, bucket) AS c, sum(tick_count)::bigint AS n
    FROM bars_1m WHERE bucket >= $1 AND bucket < $2
    GROUP BY 1, 2
), stored AS (
    SELECT symbol, bucket AS b, open AS o, high AS h, low AS l, close AS c, tick_count::bigint AS n
    FROM {view} WHERE bucket >= $1 AND bucket < $2
)
SELECT min(coalesce(f.b, s.b)) AS first, max(coalesce(f.b, s.b)) AS last, count(*) AS buckets
FROM fold f FULL JOIN stored s ON s.symbol = f.symbol AND s.b = f.b
WHERE f.symbol IS NULL OR s.symbol IS NULL
   OR f.o IS DISTINCT FROM s.o OR f.h IS DISTINCT FROM s.h OR f.l IS DISTINCT FROM s.l
   OR f.c IS DISTINCT FROM s.c OR f.n IS DISTINCT FROM s.n
"""

# Adds a bar to the hot cache only when that minute has no bar yet and the bar
# would survive the live writer's bound (newest 1,440 minutes). An absent cache
# is left absent: the leader seeds an empty series with a day of provider history
# itself, and a key holding a few recovered bars would stop it doing so.
# KEYS[1] = hist key; ARGV[1] = bound; ARGV[2..] = score, member pairs.
_CACHE_ADD_LUA = """
local key = KEYS[1]
local cap = tonumber(ARGV[1])
if redis.call('EXISTS', key) == 0 then return 0 end
local added = 0
for i = 2, #ARGV, 2 do
    local score = tonumber(ARGV[i])
    if #redis.call('ZRANGEBYSCORE', key, score, score, 'LIMIT', 0, 1) == 0 then
        local keep = true
        if redis.call('ZCARD', key) >= cap then
            local oldest_kept = redis.call('ZRANGE', key, -cap, -cap, 'WITHSCORES')
            keep = score > tonumber(oldest_kept[2])
        end
        if keep then
            redis.call('ZADD', key, score, ARGV[i + 1])
            redis.call('ZREMRANGEBYRANK', key, 0, -(cap + 1))
            added = added + 1
        end
    end
end
return added
"""


def cache_horizon(now: datetime) -> datetime:
    """The oldest minute the hot cache is for: it holds the last day of one-minute bars."""
    return floor_minute(now) - HOT_CACHE_BARS * MINUTE


def cache_member(bar: Bar, run_id: uuid.UUID) -> str:
    """The live writer's member format, plus the run that recovered it."""
    return json.dumps(
        {
            "t": int(bar.bucket.timestamp()),
            "o": bar.open,
            "h": bar.high,
            "l": bar.low,
            "c": bar.close,
            "n": 0,
            "src": "backfill",
            "run": str(run_id),
        }
    )


class RecoveryStore:
    """Every read and write a run makes, in PostgreSQL and the Redis hot cache."""

    def __init__(self, pool: asyncpg.Pool, redis: aioredis.Redis) -> None:
        self._pool = pool
        self._redis = redis

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[asyncpg.Connection | None]:
        """The run lock, or None when another run holds it.

        Session-level: the server releases it if this connection dies, so a crashed
        run never blocks the next one.
        """
        async with self._pool.acquire() as conn:
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
                yield None
                return
            try:
                yield conn
            finally:
                # If the connection died mid-run the server has already released the
                # lock with the session; an unlock error must not hide the real one.
                with contextlib.suppress(asyncpg.InterfaceError, OSError):
                    await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)

    async def cache_would_take(self, symbol: str, minutes: Sequence[datetime]) -> set[datetime]:
        """The minutes cache_add would add now - the same rules as its script, read-only.

        None when the cache is absent (it is never created here), none it already
        holds, and none older than everything a full cache keeps.
        """
        key = keys.history(symbol)
        if not minutes or not await self._redis.exists(key):
            return set()
        times = {int(m.timestamp()): m for m in minutes}
        held = cast(
            "list[tuple[str, float]]",
            await self._redis.zrangebyscore(key, min(times), max(times), withscores=True),
        )
        taken = {int(score) for _member, score in held}
        floor: float | None = None
        if await self._redis.zcard(key) >= HOT_CACHE_BARS:
            oldest_kept = cast(
                "list[tuple[str, float]]",
                await self._redis.zrange(key, -HOT_CACHE_BARS, -HOT_CACHE_BARS, withscores=True),
            )
            floor = float(oldest_kept[0][1])
        return {m for t, m in times.items() if t not in taken and (floor is None or t > floor)}

    async def schema_ready(self) -> bool:
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT to_regclass('public.history_recovery_spans') IS NOT NULL"
                    " AND EXISTS (SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = 'bars_1m' AND column_name = 'recovery_run_id')"
                )
            )

    async def present(self, symbol: str, window: Window) -> set[datetime]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT bucket FROM bars_1m WHERE symbol = $1 AND bucket BETWEEN $2 AND $3",
                symbol,
                window.start,
                window.end,
            )
        return {r["bucket"] for r in rows}

    async def settled(self, symbol: str, window: Window) -> list[tuple[datetime, datetime]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT span_start, span_end FROM history_recovery_spans"
                " WHERE symbol = $1 AND status IN ('recovered', 'partial', 'silent')"
                " AND span_end >= $2 AND span_start <= $3"
                " AND fetched_at >= span_end + $4::interval",
                symbol,
                window.start,
                window.end,
                SETTLE,
            )
        return [(r["span_start"], r["span_end"]) for r in rows]

    async def asked_since(
        self, symbol: str, window: Window, since: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Spans already asked for after ``since``, whatever the answer was."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT span_start, span_end FROM history_recovery_spans"
                " WHERE symbol = $1 AND span_end >= $2 AND span_start <= $3 AND fetched_at > $4",
                symbol,
                window.start,
                window.end,
                since,
            )
        return [(r["span_start"], r["span_end"]) for r in rows]

    async def abort_stale_runs(self, conn: asyncpg.Connection) -> int:
        # Called only while holding the advisory lock: any run still 'running' is dead.
        result = await conn.execute(
            "UPDATE history_recovery_runs SET status = 'aborted', finished_at = now(),"
            " error = coalesce(error || '; ', '') || 'the process ended before the run finished'"
            " WHERE status = 'running'"
        )
        return int(result.split()[-1])

    async def begin_run(
        self,
        run_id: uuid.UUID,
        trigger: str,
        instance: str,
        provider: str,
        window: Window,
        missing: int,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO history_recovery_runs (run_id, trigger, instance, provider,"
                " window_start, window_end, minutes_missing) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                run_id,
                trigger,
                instance,
                provider,
                window.start,
                window.end,
                missing,
            )

    async def insert(self, symbol: str, bars: Sequence[Bar], run_id: uuid.UUID) -> list[datetime]:
        """Insert-if-absent, one transaction. Returns the minutes actually inserted."""
        if not bars:
            return []
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                _INSERT_BARS,
                symbol,
                run_id,
                [b.bucket for b in bars],
                [b.open for b in bars],
                [b.high for b in bars],
                [b.low for b in bars],
                [b.close for b in bars],
            )
        return [r["bucket"] for r in rows]

    async def record_spans(
        self, run_id: uuid.UUID, results: Sequence[SpanResult], fetched_at: datetime
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO history_recovery_spans (run_id, symbol, span_start, span_end,"
                " status, minutes_missing, bars_received, bars_inserted, bars_present,"
                " minutes_silent, error, fetched_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)"
                " ON CONFLICT (run_id, symbol, span_start) DO NOTHING",
                [
                    (
                        run_id,
                        r.span.symbol,
                        r.span.start,
                        r.span.end,
                        r.status,
                        r.span.minutes,
                        r.received,
                        r.inserted,
                        r.present,
                        r.silent,
                        r.error,
                        fetched_at,
                    )
                    for r in results
                ],
            )

    async def finish_run(self, report: RunReport, error: str | None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE history_recovery_runs SET status = $2, finished_at = now(), requests = $3,"
                " bars_received = $4, bars_inserted = $5, bars_present = $6, minutes_silent = $7,"
                " cache_added = $8, error = $9 WHERE run_id = $1",
                report.run_id,
                report.status,
                report.requests,
                report.bars_received,
                report.bars_inserted,
                report.bars_present,
                report.minutes_silent,
                report.cache_added,
                error,
            )

    async def stale_rollups(
        self, window: Window, now: datetime
    ) -> list[tuple[str, datetime, datetime]]:
        """Closed buckets in the window whose rollup no longer matches bars_1m."""
        stale: list[tuple[str, datetime, datetime]] = []
        async with self._pool.acquire() as conn:
            for view, width, offset in ROLLUPS:
                bounds = refresh_range(window.start, window.end, width, offset, now)
                if bounds is None:
                    continue
                row = await conn.fetchrow(_STALE_ROLLUP.format(view=view), *bounds, width)
                if row is not None and row["buckets"]:
                    found = refresh_range(row["first"], row["last"], width, offset, now)
                    if found is not None:
                        stale.append((view, *found))
        return stale

    async def refresh_rollup(self, view: str, start: datetime, end: datetime) -> None:
        # A procedure that refuses to run inside a transaction block: plain execute.
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"CALL refresh_continuous_aggregate('{view}', $1::timestamptz, $2::timestamptz)",
                start,
                end,
            )

    async def cache_add(self, symbol: str, bars: Sequence[Bar], run_id: uuid.UUID) -> int:
        if not bars:
            return 0
        args: list[Any] = [HOT_CACHE_BARS]
        for bar in bars:
            args += [int(bar.bucket.timestamp()), cache_member(bar, run_id)]
        return int(await self._redis.eval(_CACHE_ADD_LUA, 1, keys.history(symbol), *args))

    async def recovered_since(self, symbol: str, since: datetime) -> list[tuple[Bar, uuid.UUID]]:
        """Recovered rows young enough for the hot cache, to repair it after a crash."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT bucket, open, high, low, close, recovery_run_id FROM bars_1m"
                " WHERE symbol = $1 AND bucket >= $2 AND recovery_run_id IS NOT NULL"
                " ORDER BY bucket",
                symbol,
                since,
            )
        return [
            (
                Bar(
                    symbol=symbol,
                    bucket=r["bucket"],
                    open=r["open"],
                    high=r["high"],
                    low=r["low"],
                    close=r["close"],
                    tick_count=0,
                    sum_ret=0.0,
                    sum_ret_sq=0.0,
                    source=BarSource.BACKFILL,
                ),
                r["recovery_run_id"],
            )
            for r in rows
        ]


# ------------------------------------------------------------ orchestrator --
@dataclass(frozen=True, slots=True)
class RecoveryConfig:
    lookback: timedelta = timedelta(days=7)
    # The tick stream's retention plus two minutes; see the module docstring.
    horizon: timedelta = timedelta(seconds=900 + 120)
    max_requests: int = 20
    request_spacing_s: float = 2.0
    days_per_request: int = 3
    include_settled: bool = False


class HistoryRecovery:
    """Plans and runs one recovery at a time. Every outcome is reported, none is raised
    except the database and Redis being unreachable, which the caller retries later."""

    def __init__(
        self,
        *,
        store: RecoveryStore,
        provider: Callable[[], MarketDataProvider],
        provider_name: str,
        symbols: Sequence[str],
        instance: str,
        config: RecoveryConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        stop: asyncio.Event | None = None,
        is_open: Callable[[datetime], bool] = is_market_open,
        quota: RequestQuota | None = None,
    ) -> None:
        self._store = store
        self._quota = quota
        self._provider = provider
        self._provider_name = provider_name
        self._symbols = list(symbols)
        self._instance = instance
        self._cfg = config
        self._clock = clock
        self._sleep = sleep
        self._stop = stop
        self._is_open = is_open
        self.blocked_until: datetime | None = None
        # Per symbol: the account may not read that pair's history (HTTP 403).
        self.symbol_blocked_until: dict[str, datetime] = {}

    async def run_once(
        self, trigger: str, *, apply: bool, window: Window | None = None
    ) -> RunReport:
        now = self._clock()
        if self.blocked_until is not None and now < self.blocked_until:
            return RunReport("skipped", f"provider back-off until {self.blocked_until.isoformat()}")
        window = window or recovery_window(now, self._cfg.lookback, self._cfg.horizon)
        if window is None:
            return RunReport("skipped", "empty window")
        ready = await self._store.schema_ready()
        if apply and not ready:
            return RunReport("skipped", "migration 004 is not applied", window=window)

        async with self._store.exclusive() as lock:
            if lock is None:
                return RunReport("skipped", "another recovery run holds the lock", window=window)
            report = await self._locked(lock, trigger, apply, ready, window, now)
        RUNS.labels(status=report.status).inc()
        log.info("recovery.run", trigger=trigger, **report.summary())
        return report

    async def _locked(
        self,
        lock: asyncpg.Connection,
        trigger: str,
        apply: bool,
        ready: bool,
        window: Window,
        now: datetime,
    ) -> RunReport:
        aborted = await self._store.abort_stale_runs(lock) if apply else 0
        spans, askable = await self._missing(window, ready, now)
        requests = [
            r
            for r in plan_requests(askable, self._cfg.days_per_request)
            if now >= self.symbol_blocked_until.get(r.symbol, now)
        ]
        stale = await self._store.stale_rollups(window, now)
        repairs = await self._cache_repairs(now) if ready else []

        report = RunReport(
            "dry_run" if not apply else "idle",
            window=window,
            minutes_missing=sum(s.minutes for s in spans),
            requests_planned=len(requests),
            stale_runs_aborted=aborted,
        )
        if not apply:
            report.spans = [SpanResult(s, "planned") for s in spans]
            report.rollups_refreshed = stale
            report.cache_added = len(repairs)
            return report
        if not spans and not stale and not repairs:
            report.reason = "nothing missing"
            return report
        if not askable and not stale and not repairs:
            report.reason = "every missing minute was asked for within the hour"
            return report

        report.run_id = uuid.uuid4()
        report.status = "running"
        await self._store.begin_run(
            report.run_id,
            trigger,
            self._instance,
            self._provider_name,
            window,
            report.minutes_missing,
        )
        changed, incomplete, error = await self._request_all(requests, report, now)
        await self._refresh_rollups(report, changed, stale, window, now)
        report.cache_added += await self._repair_cache(now)
        if report.status == "running":
            report.status = "partial" if incomplete else "succeeded"
        await self._store.finish_run(report, error)
        return report

    async def _missing(
        self, window: Window, ready: bool, now: datetime
    ) -> tuple[list[Span], list[Span]]:
        """Missing minutes as spans, and the part of them not asked for within SETTLE."""
        expected = open_minutes(window, self._is_open)
        spans: list[Span] = []
        askable: list[Span] = []
        for symbol in self._symbols:
            present = await self._store.present(symbol, window)
            settled: list[tuple[datetime, datetime]] = []
            asked: list[tuple[datetime, datetime]] = []
            if ready and not self._cfg.include_settled:
                settled = await self._store.settled(symbol, window)
                asked = await self._store.asked_since(symbol, window, now - SETTLE)
            spans += missing_spans(symbol, expected, present, settled)
            askable += missing_spans(symbol, expected, present, settled + asked)
        return spans, askable

    async def _request_all(
        self, requests: Sequence[Request], report: RunReport, now: datetime
    ) -> tuple[list[datetime], bool, str | None]:
        """Make the planned requests, within the run's budget. Returns (minutes inserted,
        whether anything was left undone, the last error)."""
        error: str | None = None
        incomplete = len(requests) > self._cfg.max_requests
        changed: list[datetime] = []
        refused: set[str] = set()
        for i, request in enumerate(requests[: self._cfg.max_requests]):
            if self._stop is not None and self._stop.is_set():
                report.status = "aborted"
                return changed, True, "leadership or shutdown ended the run"
            if request.symbol in refused:
                continue
            if self._quota is not None and not await self._quota.take("recovery"):
                return (
                    changed,
                    True,
                    "postponed: the provider allowance left is reserved for the feed",
                )
            if i:
                await self._sleep(self._cfg.request_spacing_s)
            outcome = await self._fetch(request, report, now)
            if isinstance(outcome, ProviderAuthError):
                report.status = "failed"
                self.blocked_until = now + AUTH_BACKOFF
                return changed, True, str(outcome)
            if isinstance(outcome, ProviderRateLimitError):
                wait = timedelta(seconds=outcome.retry_after_s or 0)
                self.blocked_until = now + max(wait, RATE_LIMIT_BACKOFF)
                return changed, True, str(outcome)
            if isinstance(outcome, ProviderEntitlementError):
                # This pair only: the others may still be readable.
                refused.add(request.symbol)
                self.symbol_blocked_until[request.symbol] = now + AUTH_BACKOFF
            if isinstance(outcome, ProviderError):
                incomplete, error = True, str(outcome)
                continue
            changed += outcome
        return changed, incomplete, error

    async def _fetch(
        self, request: Request, report: RunReport, now: datetime
    ) -> list[datetime] | ProviderError:
        """One provider request, written through. Returns the minutes inserted."""
        assert report.run_id is not None
        report.requests += 1
        try:
            bars = await self._provider().backfill(request.symbol, request.start, request.end, "1m")
        except ProviderError as exc:
            failed = [SpanResult(s, "failed", error=str(exc)) for s in request.spans]
            await self._store.record_spans(report.run_id, failed, now)
            report.spans += failed
            log.warning("recovery.request_failed", symbol=request.symbol, error=str(exc))
            return exc

        wanted = {m for s in request.spans for m in s.each_minute()}
        received = {
            b.bucket: b
            for b in bars
            if b.symbol == request.symbol
            and b.bucket in wanted
            and b.bucket == floor_minute(b.bucket)
        }
        inserted = await self._store.insert(
            request.symbol, [received[m] for m in sorted(received)], report.run_id
        )
        # The cache holds the last day; older recovered minutes live in bars_1m only.
        recent = cache_horizon(now)
        report.cache_added += await self._store.cache_add(
            request.symbol, [received[m] for m in inserted if m >= recent], report.run_id
        )
        done = set(inserted)
        results: list[SpanResult] = []
        for span in request.spans:
            got = sum(1 for m in span.each_minute() if m in received)
            new = sum(1 for m in span.each_minute() if m in done)
            status = "recovered" if got == span.minutes else "silent" if got == 0 else "partial"
            results.append(SpanResult(span, status, got, new, got - new, span.minutes - got))
        await self._store.record_spans(report.run_id, results, now)
        report.spans += results
        report.bars_received += len(received)
        report.bars_inserted += len(inserted)
        report.bars_present += len(received) - len(inserted)
        report.minutes_silent += sum(r.silent for r in results)
        BARS_RECOVERED.labels(symbol=request.symbol).inc(len(inserted))
        return inserted

    async def _refresh_rollups(
        self,
        report: RunReport,
        changed: Sequence[datetime],
        stale: Sequence[tuple[str, datetime, datetime]],
        window: Window,
        now: datetime,
    ) -> None:
        targets = list(stale)
        if changed:
            for view, width, offset in ROLLUPS:
                bounds = refresh_range(min(changed), max(changed), width, offset, now)
                if bounds is not None:
                    targets.append((view, *bounds))
        for view in {v for v, _, _ in targets}:
            starts = [a for v, a, _ in targets if v == view]
            ends = [b for v, _, b in targets if v == view]
            await self._store.refresh_rollup(view, min(starts), max(ends))
            report.rollups_refreshed.append((view, min(starts), max(ends)))

    async def _cache_repairs(self, now: datetime) -> list[tuple[Bar, uuid.UUID]]:
        """Recovered rows the hot cache lacks (a run that died before writing it)."""
        since = cache_horizon(now)
        missing: list[tuple[Bar, uuid.UUID]] = []
        for symbol in self._symbols:
            recovered = await self._store.recovered_since(symbol, since)
            wanted = await self._store.cache_would_take(symbol, [b.bucket for b, _ in recovered])
            missing += [(bar, run_id) for bar, run_id in recovered if bar.bucket in wanted]
        return missing

    async def _repair_cache(self, now: datetime) -> int:
        added = 0
        for bar, run_id in await self._cache_repairs(now):
            added += await self._store.cache_add(bar.symbol, [bar], run_id)
        return added


# --------------------------------------------------------------------- CLI --
def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


async def _cli(args: argparse.Namespace) -> int:
    cfg = IngestorSettings()
    if not cfg.database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    now = datetime.now(UTC)
    horizon = timedelta(seconds=cfg.stream_retention_s + 120)
    latest = recovery_window(now, timedelta(days=1), horizon)
    assert latest is not None
    start = floor_minute(_parse_ts(args.since))
    end = min(floor_minute(_parse_ts(args.until)), latest.end) if args.until else latest.end
    if start > end:
        print("empty window", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(cfg.database_url, min_size=1, max_size=3, command_timeout=120)
    redis = aioredis.Redis.from_url(cfg.redis_url, decode_responses=True)
    # The same ledger the running leader uses, so a manual run spends only what it may.
    quota = RequestQuota(redis, cfg.provider, cfg.quota_limits)
    try:
        recovery = HistoryRecovery(
            store=RecoveryStore(pool, redis),
            provider=lambda: build_provider(
                cfg.provider, cfg.symbol_list, token=cfg.provider_token
            ),
            provider_name=cfg.provider,
            symbols=args.symbols.split(",") if args.symbols else cfg.symbol_list,
            instance=f"cli:{socket.gethostname()}",
            config=RecoveryConfig(
                lookback=end - start + MINUTE,
                horizon=horizon,
                max_requests=args.max_requests,
                request_spacing_s=cfg.recovery_request_spacing_s,
                days_per_request=cfg.recovery_days_per_request,
                include_settled=args.include_settled,
            ),
            quota=quota,
        )
        report = await recovery.run_once("manual", apply=args.apply, window=Window(start, end))
    finally:
        await pool.close()
        await redis.aclose()
    print(json.dumps(report.summary(), indent=2))
    for result in report.spans:
        print(
            f"{result.span.symbol} {result.span.start:%Y-%m-%d %H:%M}..{result.span.end:%H:%M}"
            f" {result.span.minutes} min {result.status}"
            + (f" received {result.received} inserted {result.inserted}" if args.apply else "")
        )
    return 0 if report.status in ("dry_run", "idle", "succeeded") else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recover missed one-minute bars from provider history. Dry run unless --apply."
    )
    parser.add_argument("--since", required=True, help="first minute to examine (ISO 8601, UTC)")
    parser.add_argument("--until", help="last minute to examine (default: the newest eligible)")
    parser.add_argument("--symbols", help="comma-separated (default: FX_SYMBOLS)")
    parser.add_argument("--apply", action="store_true", help="write; without it nothing is written")
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument(
        "--include-settled",
        action="store_true",
        help="ask again for minutes an earlier run found the provider silent on",
    )
    sys.exit(asyncio.run(_cli(parser.parse_args())))


if __name__ == "__main__":
    main()
