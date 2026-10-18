"""History recovery against real PostgreSQL (migrations 001, 003, 004) and Redis.

Every bar here is dated 2030, where no real observation can exist, so the
cleanup - DELETE ... WHERE bucket in 2030 - cannot touch real data even if the
suite were pointed at the wrong database. Prices are deterministic test values,
not market data. The provider stand-in answers like Tiingo's minute endpoint:
whole UTC days, filtered to the requested range, closed-market minutes included.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any

import asyncpg
import pytest
import redis.asyncio as aioredis
from fx_core import keys
from fx_core.calendar import is_market_open
from fx_core.models import Bar, BarSource
from fx_ingestor.providers import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderError,
    ProviderRateLimitError,
)
from fx_ingestor.recovery import (
    LOCK_KEY,
    MINUTE,
    HistoryRecovery,
    RecoveryConfig,
    RecoveryStore,
    RunReport,
    Window,
)

from tests.conftest import DATABASE_URL, _unavailable

pytestmark = pytest.mark.integration

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"]
BASE = {"EURUSD": 1.10, "GBPUSD": 1.30, "USDJPY": 150.0, "AUDUSD": 0.66, "USDCHF": 0.90}
MONDAY = datetime(2030, 10, 14, tzinfo=UTC)  # a Monday in US daylight time, like 2026-10-12


def at(day: datetime, hh: int, mm: int) -> datetime:
    return day.replace(hour=hh, minute=mm)


def span(start: datetime, end: datetime) -> list[datetime]:
    out, m = [], start
    while m <= end:
        out.append(m)
        m += MINUTE
    return out


def price(symbol: str, minute: datetime, shift: float = 0.0) -> tuple[float, float, float, float]:
    """Test values: deterministic, consistent OHLC, unrelated to any market."""
    k = int(minute.timestamp()) // 60
    o = BASE[symbol] * (1 + shift + (k % 17) * 1e-5)
    c = BASE[symbol] * (1 + shift + (k % 13) * 1e-5)
    return o, max(o, c) * (1 + 2e-5), min(o, c) * (1 - 2e-5), c


class FakeHistory:
    """Provider history stand-in. ``has`` decides which minutes it holds."""

    def __init__(
        self,
        has: Callable[[str, datetime], bool] = lambda _s, _m: True,
        error: Callable[[int], ProviderError | None] = lambda _n: None,
        before: Callable[[str, datetime, datetime], Awaitable[None]] | None = None,
    ) -> None:
        self.has, self.error, self.before = has, error, before
        self.calls: list[tuple[str, datetime, datetime]] = []

    async def backfill(
        self, symbol: str, start: datetime, end: datetime, interval: str = "1m"
    ) -> list[Bar]:
        self.calls.append((symbol, start, end))
        if self.before is not None:
            await self.before(symbol, start, end)
        if (exc := self.error(len(self.calls))) is not None:
            raise exc
        bars, m = [], datetime.combine(start.date(), time(), UTC)
        last = datetime.combine(end.date(), time(23, 59), UTC)
        while m <= last:
            if start <= m <= end and self.has(symbol, m):
                o, h, low, c = price(symbol, m)
                bars.append(Bar(symbol, m, o, h, low, c, 0, 0.0, 0.0, BarSource.BACKFILL))
            m += MINUTE
        return bars


@pytest.fixture
async def pg() -> AsyncIterator[asyncpg.Pool]:
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=6)
    except (OSError, asyncpg.PostgresError) as exc:
        _unavailable(f"PostgreSQL at {DATABASE_URL}", str(exc))
    if not await pool.fetchval("SELECT to_regclass('public.history_recovery_spans') IS NOT NULL"):
        await pool.close()
        _unavailable("history_recovery_spans", "apply infra/migrations/004_history_recovery.sql")
    await _clean(pool)
    try:
        yield pool
    finally:
        await _clean(pool)
        await pool.close()


async def _clean(pool: asyncpg.Pool) -> None:
    await pool.execute("DELETE FROM history_recovery_runs WHERE window_start >= '2030-01-01'")
    await pool.execute("DELETE FROM bars_1m WHERE bucket >= '2030-01-01' AND bucket < '2031-01-01'")
    for view in ("bars_5m", "bars_1h"):
        await pool.execute(
            f"CALL refresh_continuous_aggregate('{view}', '2030-01-01', '2031-01-01')"
        )


def recovery(
    pg: asyncpg.Pool,
    redis: aioredis.Redis,
    provider: FakeHistory,
    now: datetime,
    symbols: list[str] | None = None,
    stop: asyncio.Event | None = None,
    **cfg: Any,
) -> HistoryRecovery:
    async def no_wait(_s: float) -> None:
        return None

    return HistoryRecovery(
        store=RecoveryStore(pg, redis),
        provider=lambda: provider,  # type: ignore[arg-type,return-value]
        provider_name="tiingo",
        symbols=symbols or SYMBOLS,
        instance="pytest",
        config=RecoveryConfig(request_spacing_s=0, **cfg),
        clock=lambda: now,
        sleep=no_wait,
        stop=stop,
    )


async def seed_live(pg: asyncpg.Pool, symbol: str, minutes: list[datetime]) -> None:
    """Stream rows as the worker writes them, with prices unlike the provider's."""
    rows = []
    for i, m in enumerate(minutes):
        o, h, low, c = price(symbol, m, shift=0.01)
        rows.append((symbol, m, o, h, low, c, 11, 1e-4, 1e-8, f"{1000 + i}-0"))
    await pg.executemany(
        "INSERT INTO bars_1m (symbol, bucket, open, high, low, close, tick_count, sum_ret,"
        " sum_ret_sq, source, last_stream_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,"
        " 'stream', $10)",
        rows,
    )


async def seed_cache(redis: aioredis.Redis, symbol: str, minutes: list[datetime]) -> None:
    members = {}
    for m in minutes:
        o, h, low, c = price(symbol, m, shift=0.01)
        t = int(m.timestamp())
        members[
            json.dumps({"t": t, "o": o, "h": h, "l": low, "c": c, "n": 11, "src": "stream"})
        ] = t
    await redis.zadd(keys.history(symbol), members)
    await redis.expire(keys.history(symbol), keys.TTL_HISTORY_S)


async def rows(pg: asyncpg.Pool, symbol: str, day: datetime) -> dict[datetime, asyncpg.Record]:
    found = await pg.fetch(
        "SELECT * FROM bars_1m WHERE symbol = $1 AND bucket >= $2 AND bucket < $3",
        symbol,
        day,
        day + timedelta(days=1),
    )
    return {r["bucket"]: r for r in found}


async def rollups_match_bars(pg: asyncpg.Pool, start: datetime, end: datetime) -> dict[str, int]:
    """Buckets where a rollup disagrees with a fold of bars_1m, per view (0 = consistent)."""
    out = {}
    for view, width in (("bars_5m", "5 minutes"), ("bars_1h", "1 hour")):
        out[view] = await pg.fetchval(
            f"""WITH f AS (SELECT symbol, time_bucket('{width}', bucket) b, first(open, bucket) o,
                         max(high) h, min(low) l, last(close, bucket) c, sum(tick_count) n
                         FROM bars_1m WHERE bucket >= $1 AND bucket < $2 GROUP BY 1, 2)
                SELECT count(*) FROM f FULL JOIN (SELECT * FROM {view} WHERE bucket >= $1
                AND bucket < $2) v ON v.symbol = f.symbol AND v.bucket = f.b
                WHERE f.symbol IS NULL OR v.symbol IS NULL OR f.o <> v.open OR f.h <> v.high
                   OR f.l <> v.low OR f.c <> v.close OR f.n <> v.tick_count""",
            start,
            end,
        )
    return out


# ------------------------------------------------------- the October 12 case --
async def test_the_october_12_outage_is_recovered_without_touching_live_data(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    """2026-10-12's pattern: live 10:57-11:42 and 14:36-16:46 UTC, nothing else stored."""
    live = span(at(MONDAY, 10, 57), at(MONDAY, 11, 42)) + span(
        at(MONDAY, 14, 36), at(MONDAY, 16, 46)
    )
    for s in SYMBOLS:
        await seed_live(pg, s, live)
        await seed_cache(redis, s, live)
    # The live path's state, which recovery must leave exactly as it is.
    await redis.set(
        keys.regime_state("EURUSD"), json.dumps({"trigger": {"regime": "normal", "seq": 3}})
    )
    await redis.zadd(keys.zhist("EURUSD"), {json.dumps({"t": 1, "z": 0.4}): 1})
    await redis.xadd(keys.STREAM_TICKS, {"s": "EURUSD", "b": "1.1", "a": "1.1001"})
    await redis.hset(keys.quote("EURUSD"), mapping={"bid": "1.1", "ask": "1.1001"})
    untouched = {
        k: await redis.dump(k)
        for k in (
            keys.regime_state("EURUSD"),
            keys.zhist("EURUSD"),
            keys.STREAM_TICKS,
            keys.quote("EURUSD"),
        )
    }
    live_before = {s: {m: dict(r) for m, r in (await rows(pg, s, MONDAY)).items()} for s in SYMBOLS}
    alerts_before = await pg.fetchval("SELECT count(*) FROM alert_events")
    pubsub = redis.pubsub()
    await pubsub.psubscribe("*")

    provider = FakeHistory()
    now = at(MONDAY, 0, 0) + timedelta(days=1, minutes=30)
    window = Window(at(MONDAY, 0, 0), at(MONDAY, 23, 59))
    report = await recovery(pg, redis, provider, now).run_once(
        "leadership", apply=True, window=window
    )

    gap = 1440 - len(live)
    assert report.status == "succeeded"
    assert report.minutes_missing == report.bars_received == report.bars_inserted == 5 * gap
    assert report.minutes_silent == report.bars_present == 0
    assert len(provider.calls) == 5  # one request per pair: all three spans lie in one day
    for s in SYMBOLS:
        stored = await rows(pg, s, MONDAY)
        assert len(stored) == 1440
        for m, before in live_before[s].items():
            assert dict(stored[m]) == before, "a live bar changed"
        for m in set(stored) - set(live):
            r = stored[m]
            assert (r["open"], r["high"], r["low"], r["close"]) == price(s, m)
            assert r["source"] == "backfill" and r["recovery_run_id"] == report.run_id
            # OHLC only: no tick count, no tick-level variance is claimed.
            assert r["tick_count"] == 0 and r["sum_ret"] == 0 and r["sum_ret_sq"] == 0
            assert r["last_stream_id"] == "0-0"

    spans = await pg.fetch(
        "SELECT symbol, span_start, span_end, status FROM history_recovery_spans WHERE run_id = $1"
        " ORDER BY symbol, span_start",
        report.run_id,
    )
    assert [(r["span_start"], r["span_end"]) for r in spans if r["symbol"] == "EURUSD"] == [
        (at(MONDAY, 0, 0), at(MONDAY, 10, 56)),
        (at(MONDAY, 11, 43), at(MONDAY, 14, 35)),
        (at(MONDAY, 16, 47), at(MONDAY, 23, 59)),
    ]
    assert {r["status"] for r in spans} == {"recovered"}

    # The hot cache holds the last day: live members kept, recovered minutes added from
    # 24 hours before `now` (Monday 00:30), the older half hour left to bars_1m.
    recent = {int(m.timestamp()) for m in span(at(MONDAY, 0, 30), at(MONDAY, 23, 59))}
    live_t = {int(m.timestamp()) for m in live}
    for s in SYMBOLS:
        cached = {
            c["t"]: c for c in (json.loads(v) for v in await redis.zrange(keys.history(s), 0, -1))
        }
        assert set(cached) == recent
        assert all(c["src"] == "stream" for t, c in cached.items() if t in live_t)
        assert all(
            c["src"] == "backfill" and c["n"] == 0 and c["run"] == str(report.run_id)
            for t, c in cached.items()
            if t not in live_t
        )

    assert await rollups_match_bars(pg, MONDAY, MONDAY + timedelta(days=1)) == {
        "bars_5m": 0,
        "bars_1h": 0,
    }
    for k, v in untouched.items():
        assert await redis.dump(k) == v, f"{k} changed"
    assert await pg.fetchval("SELECT count(*) FROM alert_events") == alerts_before
    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
    await pubsub.aclose()
    assert message is None, "recovered history was published as a live event"

    # The same recovery again changes nothing and asks the provider for nothing.
    again = await recovery(pg, redis, provider, now).run_once("periodic", apply=True, window=window)
    assert again.status == "idle" and again.run_id is None
    assert len(provider.calls) == 5
    assert (
        await pg.fetchval(
            "SELECT count(*) FROM history_recovery_runs WHERE window_start >= '2030-01-01'"
        )
        == 1
    )


async def test_days_of_downtime_are_recovered_without_inventing_weekend_minutes(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    friday = MONDAY - timedelta(days=3)
    window = Window(friday, at(MONDAY, 23, 59))
    provider = FakeHistory()  # it also has closed-market minutes, as Tiingo does
    report = await recovery(
        pg, redis, provider, MONDAY + timedelta(days=2), ["EURUSD"], days_per_request=3
    ).run_once("leadership", apply=True, window=window)

    stored = await pg.fetch(
        "SELECT bucket FROM bars_1m WHERE symbol = 'EURUSD' AND bucket >= $1", friday
    )
    assert report.status == "succeeded"
    assert len(stored) == report.bars_inserted == 21 * 60 + 3 * 60 + 1440
    assert all(is_market_open(r["bucket"]) for r in stored)
    # The gap from Sunday's reopen runs on into Monday, and Friday plus that would be four
    # UTC days: two requests, each within three days, and none for the closed weekend.
    assert [(c[1], c[2]) for c in provider.calls] == [
        (friday, at(friday, 20, 59)),
        (at(MONDAY - timedelta(days=1), 21, 0), at(MONDAY, 23, 59)),
    ]


# ---------------------------------------------------- what the provider lacks --
async def test_minutes_the_provider_lacks_stay_missing_until_settled(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    day = MONDAY + timedelta(days=2)
    window = Window(at(day, 10, 0), at(day, 10, 59))
    lacking = set(span(at(day, 10, 20), at(day, 10, 29)))
    provider = FakeHistory(has=lambda _s, m: m not in lacking)

    first = await recovery(pg, redis, provider, at(day, 11, 19), ["EURUSD"]).run_once(
        "periodic", apply=True, window=window
    )
    assert (first.status, first.bars_inserted, first.minutes_silent) == ("succeeded", 50, 10)
    assert [r.status for r in first.spans] == ["partial"]

    # Fetched only 20 minutes after the gap: the provider may still publish it. Ask again.
    second = await recovery(pg, redis, provider, at(day, 11, 49), ["EURUSD"]).run_once(
        "periodic", apply=True, window=window
    )
    assert second.minutes_missing == 10 and [r.status for r in second.spans] == ["silent"]

    # Fetched more than an hour after the gap: settled. Not asked for again, not invented.
    third = await recovery(pg, redis, provider, at(day, 13, 0), ["EURUSD"]).run_once(
        "periodic", apply=True, window=window
    )
    assert third.status == "idle" and len(provider.calls) == 2
    assert not await pg.fetchval(
        "SELECT count(*) FROM bars_1m WHERE symbol = 'EURUSD' AND bucket BETWEEN $1 AND $2",
        at(day, 10, 20),
        at(day, 10, 29),
    )
    recheck = await recovery(
        pg, redis, provider, at(day, 13, 0), ["EURUSD"], include_settled=True
    ).run_once("manual", apply=True, window=window)
    assert recheck.minutes_missing == 10 and len(provider.calls) == 3


async def test_a_holiday_the_provider_has_nothing_for_is_asked_about_once(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    christmas = datetime(2030, 12, 25, tzinfo=UTC)  # the calendar says open; the provider is silent
    window = Window(at(christmas, 10, 0), at(christmas, 10, 59))
    provider = FakeHistory(has=lambda _s, _m: False)

    first = await recovery(pg, redis, provider, at(christmas, 12, 0), ["EURUSD"]).run_once(
        "periodic", apply=True, window=window
    )
    later = await recovery(pg, redis, provider, at(christmas, 12, 15), ["EURUSD"]).run_once(
        "periodic", apply=True, window=window
    )

    assert (first.status, first.bars_inserted, first.minutes_silent) == ("succeeded", 0, 60)
    assert later.status == "idle" and len(provider.calls) == 1
    assert not await pg.fetchval("SELECT count(*) FROM bars_1m WHERE bucket >= $1", christmas)


# ------------------------------------------------------- crashes and leaders --
async def test_a_run_interrupted_between_postgres_and_redis_is_completed_by_the_next(
    pg: asyncpg.Pool, redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = Window(at(MONDAY, 10, 0), at(MONDAY, 10, 59))
    await seed_cache(redis, "EURUSD", span(at(MONDAY, 9, 0), at(MONDAY, 9, 59)))
    provider = FakeHistory()
    now = at(MONDAY, 11, 20)
    crashing = recovery(pg, redis, provider, now, ["EURUSD"])

    async def redis_dies(*_a: object) -> int:
        raise aioredis.ConnectionError("Redis went away")

    monkeypatch.setattr(crashing._store, "cache_add", redis_dies)
    with pytest.raises(aioredis.ConnectionError):
        await crashing.run_once("periodic", apply=True, window=window)
    assert (
        await pg.fetchval(
            "SELECT status FROM history_recovery_runs WHERE window_start = $1", window.start
        )
        == "running"
    )
    assert await redis.zcard(keys.history("EURUSD")) == 60

    resumed = await recovery(pg, redis, provider, now, ["EURUSD"]).run_once(
        "periodic", apply=True, window=window
    )

    assert resumed.stale_runs_aborted == 1 and resumed.status == "succeeded"
    assert resumed.requests == 0 and len(provider.calls) == 1  # the provider is not asked again
    assert resumed.cache_added == 60 and await redis.zcard(keys.history("EURUSD")) == 120
    statuses = await pg.fetch(
        "SELECT status FROM history_recovery_runs WHERE window_start = $1 ORDER BY started_at",
        window.start,
    )
    assert [r["status"] for r in statuses] == ["aborted", "succeeded"]


async def test_runs_never_overlap(pg: asyncpg.Pool, redis: aioredis.Redis) -> None:
    provider = FakeHistory()
    async with pg.acquire() as other:
        assert await other.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY)
        report = await recovery(pg, redis, provider, at(MONDAY, 12, 0), ["EURUSD"]).run_once(
            "periodic", apply=True, window=Window(at(MONDAY, 10, 0), at(MONDAY, 10, 59))
        )
        await other.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
    assert report.status == "skipped" and "lock" in report.reason and not provider.calls


async def test_losing_the_lease_ends_a_run_and_the_next_leader_finishes_it(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    lost = asyncio.Event()

    async def lease_lost_mid_run(*_a: object) -> None:
        lost.set()

    window = Window(at(MONDAY, 10, 0), at(MONDAY, 10, 59))
    old = await recovery(
        pg, redis, FakeHistory(before=lease_lost_mid_run), at(MONDAY, 12, 0), stop=lost
    ).run_once("periodic", apply=True, window=window)
    assert old.status == "aborted" and old.requests == 1 and old.bars_inserted == 60

    new = await recovery(pg, redis, FakeHistory(), at(MONDAY, 12, 0)).run_once(
        "leadership", apply=True, window=window
    )
    assert new.status == "succeeded" and new.minutes_missing == 4 * 60
    assert (
        await pg.fetchval(
            "SELECT count(*) FROM bars_1m WHERE bucket BETWEEN $1 AND $2", window.start, window.end
        )
        == 5 * 60
    )


# ------------------------------------------------------------- provider refusals --
async def test_refused_credentials_end_the_run_and_back_off(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    provider = FakeHistory(
        error=lambda _n: ProviderAuthError("tiingo refused the history request (HTTP 403)")
    )
    rec = recovery(pg, redis, provider, at(MONDAY, 12, 0))
    window = Window(at(MONDAY, 10, 0), at(MONDAY, 10, 59))

    report = await rec.run_once("periodic", apply=True, window=window)
    again = await rec.run_once("periodic", apply=True, window=window)

    assert report.status == "failed" and report.bars_inserted == 0 and len(provider.calls) == 1
    assert again.status == "skipped" and "back-off" in again.reason and len(provider.calls) == 1
    assert not await pg.fetchval("SELECT count(*) FROM bars_1m WHERE bucket >= $1", MONDAY)


async def test_a_pair_the_account_may_not_read_is_left_alone_and_the_rest_recovered(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    provider = FakeHistory(error=lambda n: ProviderEntitlementError("HTTP 403") if n == 2 else None)
    rec = recovery(pg, redis, provider, at(MONDAY, 12, 0), ["EURUSD", "GBPUSD", "USDJPY"])
    window = Window(at(MONDAY, 10, 0), at(MONDAY, 10, 59))

    report = await rec.run_once("periodic", apply=True, window=window)
    again = await rec.run_once("periodic", apply=True, window=window)

    assert report.status == "partial" and report.bars_inserted == 120  # EURUSD and USDJPY
    assert [c[0] for c in provider.calls] == ["EURUSD", "GBPUSD", "USDJPY"]
    # GBPUSD is still missing but not asked for again until the back-off ends.
    assert again.minutes_missing == 60 and again.requests == 0 and len(provider.calls) == 3


async def test_a_rate_limit_keeps_what_was_fetched_and_waits(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    provider = FakeHistory(
        error=lambda n: ProviderRateLimitError("HTTP 429", retry_after_s=1800) if n == 2 else None
    )
    now = at(MONDAY, 12, 0)
    rec = recovery(pg, redis, provider, now)
    report = await rec.run_once(
        "periodic", apply=True, window=Window(at(MONDAY, 10, 0), at(MONDAY, 10, 59))
    )

    assert report.status == "partial" and report.bars_inserted == 60 and len(provider.calls) == 2
    assert rec.blocked_until == now + timedelta(seconds=1800)


# ------------------------------------------------------------- live bars win --
async def test_a_live_bar_stored_during_the_run_is_kept(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    racing = at(MONDAY, 10, 30)

    async def worker_writes_first(*_a: object) -> None:
        await seed_live(pg, "EURUSD", [racing])

    report = await recovery(
        pg, redis, FakeHistory(before=worker_writes_first), at(MONDAY, 12, 0), ["EURUSD"]
    ).run_once("periodic", apply=True, window=Window(at(MONDAY, 10, 0), at(MONDAY, 10, 59)))

    row = (await rows(pg, "EURUSD", MONDAY))[racing]
    assert report.bars_inserted == 59 and report.bars_present == 1
    assert row["source"] == "stream" and row["tick_count"] == 11 and row["recovery_run_id"] is None
    assert (row["open"], row["close"]) == (
        price("EURUSD", racing, 0.01)[0],
        price("EURUSD", racing, 0.01)[3],
    )


# ------------------------------------------------------------------- rollups --
async def test_rollups_the_policies_never_refreshed_are_regenerated(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    """Like 2026-10-11 and -12: the last buckets before an outage were never materialised."""
    for s in SYMBOLS:
        await seed_live(pg, s, span(at(MONDAY, 9, 0), at(MONDAY, 10, 59)))
    now = at(MONDAY, 11, 0) + timedelta(seconds=30)

    report = await recovery(pg, redis, FakeHistory(), now).run_once(
        "periodic", apply=True, window=Window(at(MONDAY, 9, 0), at(MONDAY, 10, 59))
    )

    assert report.status == "succeeded" and report.requests == 0
    assert {v for v, _, _ in report.rollups_refreshed} == {"bars_5m", "bars_1h"}
    # Every closed bucket now matches bars_1m: 5-minute ones to 10:55, hourly ones to 10:00.
    assert (await rollups_match_bars(pg, at(MONDAY, 9, 0), at(MONDAY, 10, 55)))["bars_5m"] == 0
    assert (await rollups_match_bars(pg, at(MONDAY, 9, 0), at(MONDAY, 10, 0)))["bars_1h"] == 0
    # Buckets still open at `now` are left to the refresh policies.
    assert not await pg.fetchval(
        "SELECT count(*) FROM bars_5m WHERE bucket = $1", at(MONDAY, 10, 55)
    )
    assert not await pg.fetchval(
        "SELECT count(*) FROM bars_1h WHERE bucket = $1", at(MONDAY, 10, 0)
    )
    assert (
        await pg.fetchval("SELECT count(*) FROM bars_1h WHERE bucket = $1", at(MONDAY, 9, 0)) == 5
    )


async def test_recovered_minutes_land_in_a_compressed_chunk(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    if not await pg.fetchval(
        "SELECT compression_enabled FROM timescaledb_information.hypertables"
        " WHERE hypertable_name = 'bars_1m'"
    ):
        pytest.skip("this database does not have the columnstore enabled on bars_1m")
    day = MONDAY + timedelta(days=8)
    present = [
        m
        for m in span(at(day, 10, 0), at(day, 10, 59))
        if not at(day, 10, 10) <= m <= at(day, 10, 19)
    ]
    await seed_live(pg, "EURUSD", present)
    await pg.execute(
        "SELECT compress_chunk(c) FROM show_chunks('bars_1m') c WHERE c::regclass IN"
        " (SELECT format('%I.%I', chunk_schema, chunk_name)::regclass"
        "  FROM timescaledb_information.chunks"
        "  WHERE hypertable_name = 'bars_1m' AND range_start <= $1 AND range_end > $1)",
        at(day, 10, 0),
    )
    report = await recovery(pg, redis, FakeHistory(), at(day, 12, 0), ["EURUSD"]).run_once(
        "periodic", apply=True, window=Window(at(day, 10, 0), at(day, 10, 59))
    )
    stored = await rows(pg, "EURUSD", day)
    assert report.bars_inserted == 10 and len(stored) == 60
    assert all(stored[m]["source"] == "stream" for m in present)


# ------------------------------------------------------- dry run and the cache --
async def test_a_dry_run_writes_nothing_and_asks_the_provider_nothing(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    provider = FakeHistory()
    report = await recovery(pg, redis, provider, at(MONDAY, 12, 0)).run_once(
        "periodic", apply=False, window=Window(at(MONDAY, 10, 0), at(MONDAY, 10, 59))
    )

    assert (
        report.status == "dry_run"
        and report.minutes_missing == 300
        and report.requests_planned == 5
    )
    assert not provider.calls and report.run_id is None
    assert not await pg.fetchval("SELECT count(*) FROM bars_1m WHERE bucket >= $1", MONDAY)
    assert not await pg.fetchval(
        "SELECT count(*) FROM history_recovery_runs WHERE window_start >= $1", MONDAY
    )
    assert not await redis.keys("*")


async def test_the_hot_cache_is_never_created_or_pushed_past_its_newest_day(
    pg: asyncpg.Pool, redis: aioredis.Redis
) -> None:
    newer = span(at(MONDAY, 12, 0), at(MONDAY, 12, 0) + timedelta(minutes=1439))
    await seed_cache(redis, "GBPUSD", newer)  # full: 1,440 bars newer than anything recovered
    report = await recovery(
        pg, redis, FakeHistory(), at(MONDAY, 12, 0) + timedelta(days=1), ["EURUSD", "GBPUSD"]
    ).run_once("periodic", apply=True, window=Window(at(MONDAY, 10, 0), at(MONDAY, 10, 59)))

    assert report.bars_inserted == 120 and report.cache_added == 0
    assert not await redis.exists(keys.history("EURUSD"))  # the leader seeds an empty series itself
    cached = [json.loads(v)["t"] for v in await redis.zrange(keys.history("GBPUSD"), 0, -1)]
    assert cached == [int(m.timestamp()) for m in newer]


async def test_applying_without_migration_004_is_refused(
    pg: asyncpg.Pool, redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = recovery(pg, redis, FakeHistory(), at(MONDAY, 12, 0))

    async def not_applied() -> bool:
        return False

    monkeypatch.setattr(rec._store, "schema_ready", not_applied)
    report: RunReport = await rec.run_once(
        "periodic", apply=True, window=Window(at(MONDAY, 10, 0), at(MONDAY, 10, 59))
    )
    assert report.status == "skipped" and "004" in report.reason
