"""The provider request ledger (fx_ingestor.quota) against a real Redis.

Limits here are small test values, not the account's; clocks are controlled.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import redis.asyncio as aioredis
from fx_core import keys
from fx_core.models import Bar, BarSource
from fx_ingestor.config import IngestorSettings
from fx_ingestor.pipeline import IngestPipeline
from fx_ingestor.quota import QuotaLimits, RequestQuota

from tests.conftest import REDIS_URL

pytestmark = pytest.mark.integration

# Background work may use 6 an hour and 24 a day.
LIMITS = QuotaLimits(per_hour=10, per_day=40, reserve_per_hour=4, reserve_per_day=16)


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2030, 10, 14, 12, tzinfo=UTC).timestamp()

    def __call__(self) -> float:
        return self.t


def ledger(redis: aioredis.Redis, clock: Clock) -> RequestQuota:
    return RequestQuota(redis, "tiingo", LIMITS, clock=clock)


async def test_background_requests_stop_where_the_reserve_begins(redis: aioredis.Redis) -> None:
    quota = ledger(redis, Clock())

    granted = [await quota.take("recovery") for _ in range(8)]

    assert granted == [True] * 6 + [False] * 2
    assert await quota.used() == (6, 6)


async def test_the_feeds_requests_are_never_refused_and_leave_recovery_less(
    redis: aioredis.Redis,
) -> None:
    quota = ledger(redis, Clock())
    for _ in range(3):
        await quota.record("history_1d")
    assert [await quota.take("recovery") for _ in range(4)] == [True] * 3 + [False]

    for _ in range(12):  # past the account's own limit: counted, still made
        await quota.record("history_1d")
    assert await quota.used() == (18, 18)


async def test_the_hour_rolls(redis: aioredis.Redis) -> None:
    clock = Clock()
    quota = ledger(redis, clock)
    assert all([await quota.take("recovery") for _ in range(6)])

    clock.t += 3_599
    assert not await quota.take("recovery")
    clock.t += 2
    assert await quota.take("recovery")


async def test_the_day_caps_what_the_hours_would_allow(redis: aioredis.Redis) -> None:
    clock = Clock()
    quota = ledger(redis, clock)
    taken = 0
    for _ in range(8):
        taken += sum([await quota.take("recovery") for _ in range(6)])
        clock.t += 3_600
    assert taken == 24

    clock.t += 86_400
    assert await quota.take("recovery")
    assert await quota.used() == (1, 1)  # a day old is forgotten


async def test_concurrent_callers_in_two_processes_never_overshoot(redis: aioredis.Redis) -> None:
    other = aioredis.Redis.from_url(REDIS_URL, decode_responses=True)
    clock = Clock()
    try:
        replicas = [ledger(redis, clock), RequestQuota(other, "tiingo", LIMITS, clock=clock)]
        granted = await asyncio.gather(*(q.take("recovery") for q in replicas * 20))
    finally:
        await other.aclose()

    assert sum(granted) == 6
    assert await redis.zcard(keys.provider_requests("tiingo")) == 6


async def test_a_promoted_standby_sees_what_the_old_leader_spent(redis: aioredis.Redis) -> None:
    clock = Clock()
    old = ledger(redis, clock)
    assert all([await old.take("recovery") for _ in range(6)])

    new = ledger(redis, clock)  # a fresh process: the ledger is Redis, not memory
    assert not await new.take("recovery")
    for _ in range(10):  # its own history load on taking the lease
        await new.record("history_1d")
    assert await new.used() == (16, 16)


async def test_without_redis_the_feed_still_asks_and_recovery_waits() -> None:
    down = aioredis.Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    quota = RequestQuota(down, "tiingo", LIMITS)
    try:
        await quota.record("history_1d")
        assert not await quota.take("recovery")
    finally:
        await down.aclose()


class FakeDaily:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def backfill(
        self, symbol: str, start: datetime, end: datetime, interval: str = "1m"
    ) -> list[Bar]:
        self.calls += 1
        day = datetime(2030, 10, 14, tzinfo=UTC)
        return [Bar(symbol, day, 1.1, 1.2, 1.0, 1.15, 0, 0.0, 0.0, BarSource.BACKFILL)]


async def test_the_feeds_history_load_is_made_and_counted_when_the_allowance_is_spent(
    redis: aioredis.Redis,
) -> None:
    quota = RequestQuota(redis, "tiingo", LIMITS)
    for _ in range(LIMITS.per_hour):
        await quota.record("research")
    provider = FakeDaily()
    pipeline = IngestPipeline(redis, ["EURUSD"], quota=quota)

    written = await pipeline.refresh_daily_history(provider)  # type: ignore[arg-type]

    assert written == 1 and provider.calls == 1
    assert (await quota.used())[0] == LIMITS.per_hour + 1
    assert await redis.ttl(keys.provider_requests("tiingo")) == -1  # durable, never evicted
    assert not await quota.take("recovery")


def test_the_reserve_must_leave_part_of_the_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FX_PROVIDER_TOKEN", "test")
    monkeypatch.setenv("FX_RECOVERY_RESERVE_PER_HOUR", "50")
    with pytest.raises(ValueError, match="reserve"):
        IngestorSettings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setenv("FX_RECOVERY_RESERVE_PER_HOUR", "30")
    limits = IngestorSettings(_env_file=None).quota_limits  # type: ignore[call-arg]
    assert limits == QuotaLimits(50, 1_000, 30, 400)  # recovery: 20 an hour, 600 a day
