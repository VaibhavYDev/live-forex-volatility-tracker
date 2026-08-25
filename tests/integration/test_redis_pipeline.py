"""End-to-end against a real Redis: ingest -> WAL -> persister recovery -> fan-out.

The interesting behaviour here lives in Redis semantics (consumer group pending
lists, XAUTOCLAIM idle windows, lease expiry, MINID trimming). Mocking any of it
would mean asserting our own assumptions back at ourselves.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
from fx_core import keys
from fx_core.models import Bar, Tick
from fx_ingestor.leader import LeaderLease
from fx_ingestor.pipeline import IngestPipeline
from fx_worker.persister import Persister

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SYMBOLS = ["EURUSD", "GBPUSD"]


class StubDatabase:
    """In-memory stand-in that records what the persister asked it to write.

    Lets the whole crash-recovery path be tested without a Postgres container,
    and - more usefully - lets us make the commit FAIL on demand, which is the
    branch that decides whether a worker crash loses data.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, datetime], Bar] = {}
        self.watermarks: dict[tuple[str, datetime], str] = {}
        self.calls = 0
        self.fail_next = False

    async def upsert_bars(self, bars: list[tuple[Bar, str]]) -> int:
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("simulated Postgres outage")

        for bar, stream_id in bars:
            key = (bar.symbol, bar.bucket)
            existing = self.watermarks.get(key)
            # Mirrors the SQL: `WHERE EXCLUDED.last_stream_id > bars_1m.last_stream_id`
            if existing is not None and stream_id <= existing:
                continue
            prior = self.rows.get(key)
            self.rows[key] = bar if prior is None else prior.merge(bar)
            self.watermarks[key] = stream_id
        return len(bars)


def _tick(symbol: str, price: float, ts: datetime, seq: int) -> Tick:
    return Tick(
        symbol=symbol,
        bid=price - 0.00005,
        ask=price + 0.00005,
        ts_event=ts,
        ts_ingest=ts,
        seq=seq,
    )


class TestIngestPipeline:
    async def test_one_tick_writes_wal_cache_and_fanout(self, redis: aioredis.Redis) -> None:
        pubsub = redis.pubsub()
        await pubsub.subscribe(keys.channel_tick("EURUSD"))
        await asyncio.sleep(0.05)

        pipeline = IngestPipeline(redis, SYMBOLS, retention_s=900)
        await pipeline.handle(_tick("EURUSD", 1.0842, datetime.now(UTC), 1))

        assert await redis.xlen(keys.STREAM_TICKS) == 1
        quote = await redis.hgetall(keys.quote("EURUSD"))
        assert float(quote["mid"]) == pytest.approx(1.0842)

        message = None
        for _ in range(40):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message:
                break
        assert message is not None, "tick was not published to the fan-out channel"
        await pubsub.aclose()

    async def test_durable_keys_have_no_ttl_disposable_keys_do(self, redis: aioredis.Redis) -> None:
        """The invariant that makes `volatile-lru` safe, asserted against real Redis.

        Under volatile-lru Redis only evicts keys that carry a TTL. If the WAL
        ever acquired one, an eviction could silently delete unacknowledged
        ticks - so this test is the tripwire.
        """
        pipeline = IngestPipeline(redis, SYMBOLS)
        await pipeline.handle(_tick("EURUSD", 1.0842, datetime.now(UTC), 1))

        assert await redis.ttl(keys.STREAM_TICKS) == -1  # -1 == no expiry
        assert await redis.ttl(keys.quote("EURUSD")) > 0

    async def test_wal_is_trimmed_by_time_not_count(self, redis: aioredis.Redis) -> None:
        """`MINID ~` retention: "keep 15 minutes", not "keep N entries".

        A count-based cap means something completely different at 20 ticks/sec
        than at 2_000, so it cannot be reasoned about during an incident.
        """
        pipeline = IngestPipeline(redis, SYMBOLS, retention_s=1)
        now = datetime.now(UTC)
        # `~` trims by whole macro-nodes (stream-node-max-entries, 100 by default),
        # which is exactly what keeps XADD O(1) amortised instead of O(n). So the
        # stream must span several nodes before ANY trimming is observable - a
        # smaller test would "fail" against entirely correct behaviour.
        for i in range(300):
            await pipeline.handle(_tick("EURUSD", 1.0842 + i * 1e-5, now, i))
        assert await redis.xlen(keys.STREAM_TICKS) == 300

        await asyncio.sleep(1.2)
        for i in range(300, 600):
            await pipeline.handle(_tick("EURUSD", 1.0842 + i * 1e-5, datetime.now(UTC), i))

        length = await redis.xlen(keys.STREAM_TICKS)
        assert length < 600, "old entries were never trimmed"
        # Approximate, not exact: we do not assert it equals 300.
        assert length >= 300

    async def test_bars_seal_on_bucket_boundary(self, redis: aioredis.Redis) -> None:
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)
        base = datetime(2026, 8, 19, 12, 0, 5, tzinfo=UTC)
        for i in range(5):
            await pipeline.handle(
                _tick("EURUSD", 1.0842 + i * 1e-4, base + timedelta(seconds=i), i)
            )
        # First tick of the NEXT minute seals the previous bar.
        await pipeline.handle(_tick("EURUSD", 1.0850, base + timedelta(seconds=60), 99))

        history = await redis.zrange(keys.history("EURUSD"), 0, -1)
        assert len(history) == 1


class TestPersisterRecovery:
    async def _seed(self, redis: aioredis.Redis, count: int) -> None:
        pipeline = IngestPipeline(redis, SYMBOLS)
        now = datetime.now(UTC)
        for i in range(count):
            await pipeline.handle(_tick("EURUSD", 1.0842 + i * 1e-5, now, i))

    async def test_ack_only_after_commit_means_a_crash_loses_nothing(
        self, redis: aioredis.Redis
    ) -> None:
        """The single most important guarantee in the system.

        Commit fails -> we do NOT ack -> the entries stay in the pending list ->
        a later worker picks them up. Acking before the commit would turn every
        worker crash into silent data loss.
        """
        await self._seed(redis, 20)
        db = StubDatabase()
        persister = Persister(redis, db, consumer_name="w1", flush_max_rows=100)  # type: ignore[arg-type]
        await persister.ensure_group()

        resp = await redis.xreadgroup(keys.CONSUMER_GROUP, "w1", {keys.STREAM_TICKS: ">"}, count=20)
        for entry_id, fields in resp[0][1]:
            persister._ingest_entry(entry_id, fields)
            persister._unacked.append(entry_id)

        db.fail_next = True
        with pytest.raises(ConnectionError):
            await persister.flush()

        pending = await redis.xpending(keys.STREAM_TICKS, keys.CONSUMER_GROUP)
        assert pending["pending"] == 20, "entries were acked despite the commit failing"

        # Retry succeeds and the same statistics are re-merged safely.
        await persister.flush()
        pending = await redis.xpending(keys.STREAM_TICKS, keys.CONSUMER_GROUP)
        assert pending["pending"] == 0
        assert db.rows

    async def test_dead_worker_backlog_is_adopted_by_a_survivor(
        self, redis: aioredis.Redis
    ) -> None:
        """Without XAUTOCLAIM these entries are lost forever, silently.

        Nothing errors. No log line appears. The data is simply never written,
        and you discover it as a hole in a chart three weeks later.
        """
        await self._seed(redis, 15)
        db = StubDatabase()

        dead = Persister(redis, db, consumer_name="dead-worker")  # type: ignore[arg-type]
        await dead.ensure_group()
        await redis.xreadgroup(
            keys.CONSUMER_GROUP, "dead-worker", {keys.STREAM_TICKS: ">"}, count=15
        )
        # ...and now that worker dies without acking anything.
        assert (await redis.xpending(keys.STREAM_TICKS, keys.CONSUMER_GROUP))["pending"] == 15

        survivor = Persister(redis, db, consumer_name="survivor", claim_idle_ms=0)  # type: ignore[arg-type]
        claimed = await survivor.claim_abandoned()

        assert claimed == 15
        assert (await redis.xpending(keys.STREAM_TICKS, keys.CONSUMER_GROUP))["pending"] == 0

    async def test_redelivery_does_not_double_count(self, redis: aioredis.Redis) -> None:
        """At-least-once delivery, exactly-once effect, via the row watermark."""
        await self._seed(redis, 10)
        db = StubDatabase()
        persister = Persister(redis, db, consumer_name="w1")  # type: ignore[arg-type]
        await persister.ensure_group()

        resp = await redis.xreadgroup(keys.CONSUMER_GROUP, "w1", {keys.STREAM_TICKS: ">"}, count=10)
        entries = resp[0][1]
        for entry_id, fields in entries:
            persister._ingest_entry(entry_id, fields)
            persister._unacked.append(entry_id)
        await persister.flush()

        counts_after_first = {k: b.tick_count for k, b in db.rows.items()}

        # Replay the identical batch, exactly as a crash-before-XACK would.
        replay = Persister(redis, db, consumer_name="w2")  # type: ignore[arg-type]
        for entry_id, fields in entries:
            replay._ingest_entry(entry_id, fields)
            replay._unacked.append(entry_id)
        await replay.flush()

        assert {k: b.tick_count for k, b in db.rows.items()} == counts_after_first

    async def test_malformed_entry_is_skipped_not_retried_forever(
        self, redis: aioredis.Redis
    ) -> None:
        """A poison message that is retried forever is a worse outage than a lost tick."""
        await redis.xadd(
            keys.STREAM_TICKS, {"s": "EURUSD", "b": "not-a-number", "a": "x", "t": "?"}
        )
        await self._seed(redis, 3)

        db = StubDatabase()
        persister = Persister(redis, db, consumer_name="w1")  # type: ignore[arg-type]
        await persister.ensure_group()
        resp = await redis.xreadgroup(keys.CONSUMER_GROUP, "w1", {keys.STREAM_TICKS: ">"}, count=10)
        for entry_id, fields in resp[0][1]:
            persister._ingest_entry(entry_id, fields)
            persister._unacked.append(entry_id)
        await persister.flush()

        assert (await redis.xpending(keys.STREAM_TICKS, keys.CONSUMER_GROUP))["pending"] == 0
        assert db.rows  # the good ticks still landed


class TestLeaderLease:
    async def test_only_one_instance_holds_the_feed(self, redis: aioredis.Redis) -> None:
        """The bug that breaks horizontal scaling in every naive implementation."""
        a = LeaderLease(redis, ttl_ms=2000, renew_ms=500)
        b = LeaderLease(redis, ttl_ms=2000, renew_ms=500)

        async with a.hold():
            assert a.is_leader
            # B must NOT acquire while A holds it.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(b.hold().__aenter__(), timeout=0.8)
            assert not b.is_leader

    async def test_standby_promotes_after_the_holder_releases(self, redis: aioredis.Redis) -> None:
        a = LeaderLease(redis, ttl_ms=2000, renew_ms=500)
        async with a.hold():
            pass  # clean exit releases immediately rather than waiting out the TTL

        b = LeaderLease(redis, ttl_ms=2000, renew_ms=500)
        async with asyncio.timeout(2.0), b.hold():
            assert b.is_leader

    async def test_lease_expires_if_the_holder_stops_renewing(self, redis: aioredis.Redis) -> None:
        """Failover is bounded by the TTL even when the holder dies unclean."""
        await redis.set(keys.LEADER_LEASE, "crashed-instance", px=300)
        standby = LeaderLease(redis, ttl_ms=2000, renew_ms=500)
        async with asyncio.timeout(3.0), standby.hold():
            assert standby.is_leader

    async def test_renew_interval_must_fit_twice_inside_the_ttl(
        self, redis: aioredis.Redis
    ) -> None:
        """Otherwise one dropped packet costs you leadership while perfectly healthy."""
        with pytest.raises(ValueError, match="at least two renewal attempts"):
            LeaderLease(redis, ttl_ms=1000, renew_ms=900)
