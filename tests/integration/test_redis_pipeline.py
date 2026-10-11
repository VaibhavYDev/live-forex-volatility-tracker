"""End-to-end against a real Redis: ingest -> WAL -> persister recovery -> fan-out.

The interesting behaviour here lives in Redis semantics (consumer group pending
lists, XAUTOCLAIM idle windows, lease expiry, MINID trimming). Mocking any of it
would mean asserting our own assumptions back at ourselves.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
from fx_core import keys
from fx_core.models import Bar, BarSource, Tick, bars_per_year
from fx_ingestor.config import IngestorSettings
from fx_ingestor.main import Ingestor
from fx_ingestor.pipeline import IngestPipeline
from fx_ingestor.providers import ProviderError
from fx_platform import LeaderLease, close_redis
from fx_worker.persister import Persister, run_forever

from tests.conftest import REDIS_URL

pytestmark = pytest.mark.integration

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
            if existing is not None and _order(stream_id) <= _order(existing):
                continue
            prior = self.rows.get(key)
            self.rows[key] = bar if prior is None else prior.merge(bar)
            self.watermarks[key] = stream_id
        return len(bars)


def _order(stream_id: str) -> tuple[int, int]:
    ms, seq = stream_id.split("-")
    return int(ms), int(seq)


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


class StubHistory:
    """The provider's history endpoint, answering per interval or failing."""

    def __init__(
        self,
        minutes: list[Bar] | None = None,
        days: list[Bar] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.served = {"1m": minutes or [], "1d": days or []}
        self.error = error

    async def backfill(
        self, symbol: str, start: datetime, end: datetime, interval: str = "1m"
    ) -> list[Bar]:
        if self.error is not None:
            raise self.error
        return [b for b in self.served[interval] if start <= b.bucket <= end]


class TestProviderIsolation:
    """Live mode never invents a past, and no provider is served another's."""

    STATE = (keys.history("USDJPY"), keys.zhist("USDJPY"), keys.regime_state("USDJPY"))

    async def _ingest(self, history: StubHistory | None = None) -> None:
        """The real ingestor's rehydration, with Tiingo's history endpoint stubbed."""
        ingestor = Ingestor(
            IngestorSettings(
                FX_PROVIDER="tiingo",
                FX_PROVIDER_TOKEN="t",
                FX_SYMBOLS="USDJPY",
                REDIS_URL=REDIS_URL,
            )
        )
        ingestor._build_provider = lambda: history or StubHistory()  # type: ignore[assignment,method-assign,return-value]
        try:
            await ingestor._rehydrate()
        finally:
            await close_redis(ingestor.redis)

    async def _switch(self, redis: aioredis.Redis, provider: str) -> IngestPipeline:
        pipeline = IngestPipeline(redis, ["USDJPY"])
        await pipeline.adopt_provider(provider)
        await pipeline.restore_detectors()
        return pipeline

    async def _sources(self, redis: aioredis.Redis, key: str) -> set[str]:
        return {json.loads(m)["src"] for m in await redis.zrange(key, 0, -1)}

    async def test_a_live_feed_starts_with_only_its_providers_history(
        self, redis: aioredis.Redis
    ) -> None:
        recorded = Bar(
            "USDJPY",
            datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=5),
            158.3,
            158.4,
            158.2,
            158.35,
            0,
            0.0,
            0.0,
            BarSource.BACKFILL,
        )

        await self._ingest(StubHistory(minutes=[recorded], days=[_day(1, 158.1)]))

        for interval, close in (("1m", 158.35), ("1d", 158.1)):
            stored = await redis.zrange(keys.history("USDJPY", interval), 0, -1)
            assert [(json.loads(m)["c"], json.loads(m)["src"]) for m in stored] == [
                (close, "backfill")
            ]
        assert await redis.get(keys.PROVIDER) == "tiingo"

    async def test_history_the_provider_cannot_give_stays_absent(
        self, redis: aioredis.Redis
    ) -> None:
        await self._ingest(StubHistory(error=ProviderError("403: history not in plan")))

        assert await redis.exists(keys.history("USDJPY"), keys.history("USDJPY", "1d")) == 0

    async def test_provider_history_never_replaces_observed_bars(
        self, redis: aioredis.Redis
    ) -> None:
        await redis.set(keys.PROVIDER, "tiingo")
        await redis.zadd(keys.history("USDJPY"), {_bar_json(158.344, "stream"): 1791561180})
        recorded = Bar(
            "USDJPY",
            datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=5),
            1.0,
            1.0,
            1.0,
            1.0,
            0,
            0.0,
            0.0,
            BarSource.BACKFILL,
        )

        await self._ingest(StubHistory(minutes=[recorded]))

        assert await self._sources(redis, keys.history("USDJPY")) == {"stream"}

    async def test_switching_provider_parks_the_previous_state(self, redis: aioredis.Redis) -> None:
        """An installation that ran the retired simulator, then starts on Tiingo."""
        await redis.set(keys.PROVIDER, "replay")
        await redis.zadd(keys.history("USDJPY"), {_bar_json(147.2, "stream"): 1791561180})
        await redis.set(keys.regime_state("USDJPY"), "{}")
        await redis.hset(keys.quote("USDJPY"), mapping={"mid": "147.2"})
        await redis.hset(keys.vol_state("USDJPY", "1h"), mapping={"sigma": "0.1"})

        await self._ingest()

        assert await redis.exists(*self.STATE) == 0
        assert await redis.exists(keys.quote("USDJPY"), keys.vol_state("USDJPY", "1h")) == 0
        parked = await redis.zrange(keys.archive("replay", keys.history("USDJPY")), 0, -1)
        assert [json.loads(m)["c"] for m in parked] == [147.2]
        assert await redis.get(keys.archive("replay", keys.regime_state("USDJPY"))) == "{}"

    async def test_switching_back_restores_each_providers_own_state(
        self, redis: aioredis.Redis
    ) -> None:
        await self._switch(redis, "tiingo")
        await redis.zadd(keys.history("USDJPY"), {_bar_json(158.344, "stream"): 1791561180})
        await self._switch(redis, "other")
        await redis.zadd(keys.history("USDJPY"), {_bar_json(158.1, "stream"): 1791561240})

        await self._switch(redis, "tiingo")

        live = await redis.zrange(keys.history("USDJPY"), 0, -1)
        assert [json.loads(m)["c"] for m in live] == [158.344]
        other = await redis.zrange(keys.archive("other", keys.history("USDJPY")), 0, -1)
        assert [json.loads(m)["c"] for m in other] == [158.1]

    async def test_restarting_with_the_same_provider_keeps_its_history(
        self, redis: aioredis.Redis
    ) -> None:
        await self._ingest()
        await redis.zadd(keys.history("USDJPY"), {_bar_json(158.344, "stream"): 1791561180})

        await self._ingest()

        assert await self._sources(redis, keys.history("USDJPY")) == {"stream"}
        assert not [k async for k in redis.scan_iter("archive:*")]

    async def test_history_of_unknown_provenance_is_parked_not_reused(
        self, redis: aioredis.Redis
    ) -> None:
        """The installation that predates the marker, holding Tiingo bars stored at 500079."""
        await redis.zadd(keys.history("USDJPY"), {_bar_json(500079.172, "stream"): 1791561180})
        await redis.set(keys.regime_state("USDJPY"), "{}")

        await self._ingest()

        assert await redis.exists(*self.STATE) == 0
        kept = await redis.zrange(keys.archive("unverified", keys.history("USDJPY")), 0, -1)
        assert [json.loads(m)["c"] for m in kept] == [500079.172]

    async def test_an_occupied_archive_is_never_overwritten(self, redis: aioredis.Redis) -> None:
        """A second unmarked start would otherwise replace the first parked history."""
        parked = keys.archive("unverified", keys.history("USDJPY"))
        await redis.zadd(parked, {_bar_json(500079.172, "stream"): 1791561180})
        await redis.zadd(keys.history("USDJPY"), {_bar_json(158.344, "stream"): 1791561180})

        with pytest.raises(aioredis.ResponseError, match="refusing to overwrite"):
            await self._ingest()

        assert await redis.zcard(parked) == await redis.zcard(keys.history("USDJPY")) == 1
        assert await redis.get(keys.PROVIDER) is None

    async def test_a_new_feed_skips_the_minute_the_previous_one_wrote(
        self, redis: aioredis.Redis
    ) -> None:
        """bars_1m is keyed by (symbol, bucket): a shared minute merges both feeds' prices."""
        last = datetime(2026, 10, 9, 15, 52, 40, tzinfo=UTC)
        await redis.xadd(
            keys.STREAM_TICKS,
            {"s": "USDJPY", "b": "147.2", "a": "147.21", "t": last.isoformat(), "q": 1, "p": ""},
        )
        await redis.set(keys.PROVIDER, "replay")
        pipeline = IngestPipeline(redis, ["USDJPY"])
        await pipeline.adopt_provider("tiingo")

        for seq, offset in enumerate((10, 20)):
            await pipeline.handle(_tick("USDJPY", 158.344, last + timedelta(seconds=offset), seq))

        written = [fields for _, fields in await redis.xrange(keys.STREAM_TICKS)][1:]
        assert [(f["t"], f["p"]) for f in written] == [
            ((last + timedelta(seconds=20)).isoformat(), "tiingo")
        ]

    async def test_a_new_feed_continues_the_transition_sequence(
        self, redis: aioredis.Redis
    ) -> None:
        """alert_events is unique on (symbol, seq): a reused seq drops the new alert."""

        async def start(provider: str, issued: int) -> IngestPipeline:
            previous = IngestPipeline(redis, ["USDJPY"])
            previous.state_for("USDJPY").detector.trigger.seq = issued
            snapshot = previous.state_for("USDJPY").detector.snapshot()
            await redis.set(keys.regime_state("USDJPY"), json.dumps(snapshot))
            pipeline = IngestPipeline(redis, ["USDJPY"])
            await pipeline.adopt_provider(provider)
            await pipeline.restore_detectors()
            return pipeline

        await redis.set(keys.PROVIDER, "other")
        live = await start("tiingo", issued=7)
        assert live.state_for("USDJPY").detector.trigger.seq == 7

        back = await start("other", issued=12)
        assert back.state_for("USDJPY").detector.trigger.seq == 12


class TestDailyHistory:
    """The provider's daily series stays current while the feed runs."""

    KEY = keys.history("USDJPY", "1d")

    async def _closes(self, redis: aioredis.Redis) -> list[float]:
        return [json.loads(m)["c"] for m in await redis.zrange(self.KEY, 0, -1)]

    async def test_a_missing_series_is_loaded_whole(self, redis: aioredis.Redis) -> None:
        await IngestPipeline(redis, ["USDJPY"]).refresh_daily_history(
            StubHistory(days=[_day(400, 150.0), _day(1, 158.0)])
        )

        assert await self._closes(redis) == [150.0, 158.0]

    async def test_a_refresh_replaces_recent_days_and_renews_the_expiry(
        self, redis: aioredis.Redis
    ) -> None:
        pipeline = IngestPipeline(redis, ["USDJPY"])
        await pipeline.refresh_daily_history(StubHistory(days=[_day(400, 150.0), _day(1, 158.0)]))
        await redis.expire(self.KEY, 60)

        await pipeline.refresh_daily_history(StubHistory(days=[_day(1, 158.5), _day(0, 159.0)]))

        assert await self._closes(redis) == [150.0, 158.5, 159.0]
        assert await redis.ttl(self.KEY) > 3600

    async def test_a_failed_refresh_keeps_the_stored_series(self, redis: aioredis.Redis) -> None:
        pipeline = IngestPipeline(redis, ["USDJPY"])
        await pipeline.refresh_daily_history(StubHistory(days=[_day(1, 158.0)]))

        await pipeline.refresh_daily_history(StubHistory(error=ProviderError("timeout")))

        assert await self._closes(redis) == [158.0]


def _day(ago: int, close: float) -> Bar:
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return Bar(
        "USDJPY",
        midnight - timedelta(days=ago),
        close,
        close,
        close,
        close,
        0,
        0.0,
        0.0,
        BarSource.BACKFILL,
    )


def _bar_json(price: float, src: str) -> str:
    return json.dumps(
        {"t": 1791561180, "o": price, "h": price, "l": price, "c": price, "n": 1, "src": src}
    )


class TestVolatilityAnnualisation:
    async def test_the_ewma_is_annualised_at_the_observed_tick_rate(
        self, redis: aioredis.Redis
    ) -> None:
        """Alternating +-r log returns at 10 ticks/s: the true per-minute variance is 600 r^2."""
        pipeline = IngestPipeline(redis, ["EURUSD"])
        start = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
        r, price = 1e-5, 1.0842
        for i in range(601):  # 600 ticks fill the minute; the 601st seals it
            price *= math.exp(r if i % 2 else -r)
            await pipeline.handle(_tick("EURUSD", price, start + timedelta(seconds=i / 10), i))

        vol = await redis.hgetall(keys.vol_state("EURUSD", "1h"))
        expected = r * math.sqrt(600 * bars_per_year(60))
        assert json.loads(vol["sigma_annualized"]) == pytest.approx(expected, rel=1e-6)


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

    async def test_an_open_bucket_flushed_twice_is_counted_once(
        self, redis: aioredis.Redis
    ) -> None:
        """The upsert adds tick_count; each flush must carry only new ticks."""
        await self._seed(redis, 10)
        db = StubDatabase()
        persister = Persister(redis, db, consumer_name="w1")  # type: ignore[arg-type]
        await persister.ensure_group()

        for _ in range(2):
            resp = await redis.xreadgroup(
                keys.CONSUMER_GROUP, "w1", {keys.STREAM_TICKS: ">"}, count=5
            )
            for entry_id, fields in resp[0][1]:
                persister._ingest_entry(entry_id, fields)
                persister._unacked.append(entry_id)
            await persister.flush()

        assert sum(b.tick_count for b in db.rows.values()) == 10

    async def test_a_failed_flush_then_restart_does_not_double_count(
        self, redis: aioredis.Redis
    ) -> None:
        """run_forever re-enters run(), which replays the PEL over in-memory state."""
        await self._seed(redis, 10)
        db = StubDatabase()
        persister = Persister(redis, db, consumer_name="w1")  # type: ignore[arg-type]
        await persister.ensure_group()

        resp = await redis.xreadgroup(keys.CONSUMER_GROUP, "w1", {keys.STREAM_TICKS: ">"}, count=10)
        for entry_id, fields in resp[0][1]:
            persister._ingest_entry(entry_id, fields)
            persister._unacked.append(entry_id)
        db.fail_next = True
        with pytest.raises(ConnectionError):
            await persister.flush()

        await persister.drain_own_pending()

        assert sum(b.tick_count for b in db.rows.values()) == 10
        assert (await redis.xpending(keys.STREAM_TICKS, keys.CONSUMER_GROUP))["pending"] == 0

    async def test_a_return_never_spans_two_providers(self, redis: aioredis.Redis) -> None:
        """Replay's last price to Tiingo's first is the gap between two feeds, not a move."""
        db = StubDatabase()
        persister = Persister(redis, db, consumer_name="w1")  # type: ignore[arg-type]
        minute = datetime(2026, 10, 9, 15, 53, tzinfo=UTC)
        for entry_id, price, ts, provider in (
            ("1-0", 147.2, minute - timedelta(seconds=5), "replay"),
            ("2-0", 158.344, minute, "tiingo"),
            ("3-0", 158.345, minute + timedelta(seconds=5), "tiingo"),
        ):
            persister._ingest_entry(
                entry_id,
                {
                    "s": "USDJPY",
                    "b": str(price),
                    "a": str(price),
                    "t": ts.isoformat(),
                    "p": provider,
                },
            )
        await persister.flush()

        live = db.rows[("USDJPY", minute)]
        assert live.sum_ret_sq == pytest.approx(math.log(158.345 / 158.344) ** 2)

    async def _wait_for(self, db: StubDatabase, ticks: int) -> None:
        for _ in range(100):
            if sum(b.tick_count for b in db.rows.values()) == ticks:
                return
            await asyncio.sleep(0.05)

    async def test_a_standby_adopts_a_crashed_holders_backlog_before_new_ticks(
        self, redis: aioredis.Redis
    ) -> None:
        """Adopted late, the backlog sits below a watermark that newer ticks advanced."""
        await self._seed(redis, 10)
        db = StubDatabase()
        crashed = Persister(redis, db, consumer_name="crashed")  # type: ignore[arg-type]
        await crashed.ensure_group()
        await redis.xreadgroup(keys.CONSUMER_GROUP, "crashed", {keys.STREAM_TICKS: ">"}, count=5)
        await redis.set(keys.PERSISTER_LEASE, "crashed", px=300)  # never renewed

        standby = Persister(redis, db, consumer_name="standby", flush_max_seconds=0.0)  # type: ignore[arg-type]
        lease = LeaderLease(redis, keys.PERSISTER_LEASE, ttl_ms=2000, renew_ms=500)
        task = asyncio.create_task(run_forever(standby, lease))
        await self._wait_for(db, 10)
        standby.stop()
        await task
        # The periodic sweep, once anything left behind passes claim_idle_ms.
        standby._claim_idle_ms = 0
        await standby.claim_abandoned()

        assert sum(b.tick_count for b in db.rows.values()) == 10
        assert (await redis.xpending(keys.STREAM_TICKS, keys.CONSUMER_GROUP))["pending"] == 0

    async def test_only_the_lease_holder_consumes(self, redis: aioredis.Redis) -> None:
        """Two workers starting together: one persists, the other never reads."""
        await self._seed(redis, 10)
        db = StubDatabase()
        workers = [
            Persister(redis, db, consumer_name=name, flush_max_seconds=0.0)  # type: ignore[arg-type]
            for name in ("a", "b")
        ]
        leases = [
            LeaderLease(redis, keys.PERSISTER_LEASE, ttl_ms=2000, renew_ms=500) for _ in workers
        ]
        tasks = [
            asyncio.create_task(run_forever(w, le)) for w, le in zip(workers, leases, strict=True)
        ]
        await self._wait_for(db, 10)

        consumers = await redis.xinfo_consumers(keys.STREAM_TICKS, keys.CONSUMER_GROUP)
        assert [le.is_leader for le in leases].count(True) == 1
        assert len(consumers) == 1
        assert sum(b.tick_count for b in db.rows.values()) == 10

        for worker, lease, task in zip(workers, leases, tasks, strict=True):
            worker.stop()
            if not lease.is_leader:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_a_holder_that_loses_the_lease_stops_without_writing(
        self, redis: aioredis.Redis
    ) -> None:
        """Its batch stays pending for the successor rather than racing it."""
        await self._seed(redis, 10)
        db = StubDatabase()
        holder = Persister(redis, db, consumer_name="holder", flush_max_seconds=60.0)  # type: ignore[arg-type]
        await holder.ensure_group()
        lease = LeaderLease(redis, keys.PERSISTER_LEASE, ttl_ms=2000, renew_ms=100)
        task = asyncio.create_task(run_forever(holder, lease))
        for _ in range(100):
            if (await redis.xpending(keys.STREAM_TICKS, keys.CONSUMER_GROUP))["pending"] == 10:
                break
            await asyncio.sleep(0.05)

        await redis.set(keys.PERSISTER_LEASE, "successor", px=60_000)
        await asyncio.wait_for(lease.lost.wait(), timeout=2)
        await asyncio.sleep(1.5)  # past one blocking read, where the loop checks the loss

        assert not db.rows
        assert (await redis.xpending(keys.STREAM_TICKS, keys.CONSUMER_GROUP))["pending"] == 10
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_the_watermark_is_the_numerically_highest_stream_id(
        self, redis: aioredis.Redis
    ) -> None:
        """Stream ids are <ms>-<seq> with an unpadded seq: as text, ...-10 < ...-9."""
        await redis.xgroup_create(keys.STREAM_TICKS, keys.CONSUMER_GROUP, id="0", mkstream=True)
        ts = datetime.now(UTC).isoformat()
        for seq in (9, 10):
            await redis.xadd(
                keys.STREAM_TICKS,
                {"s": "EURUSD", "b": "1.0841", "a": "1.0843", "t": ts},
                id=f"1791557180174-{seq}",
            )
        db = StubDatabase()
        persister = Persister(redis, db, consumer_name="w1")  # type: ignore[arg-type]
        resp = await redis.xreadgroup(keys.CONSUMER_GROUP, "w1", {keys.STREAM_TICKS: ">"})
        for entry_id, fields in resp[0][1]:
            persister._ingest_entry(entry_id, fields)
            persister._unacked.append(entry_id)
        await persister.flush()

        assert list(db.watermarks.values()) == ["1791557180174-10"]

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
        a = LeaderLease(redis, keys.LEADER_LEASE, ttl_ms=2000, renew_ms=500)
        b = LeaderLease(redis, keys.LEADER_LEASE, ttl_ms=2000, renew_ms=500)

        async with a.hold():
            assert a.is_leader
            # B must NOT acquire while A holds it.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(b.hold().__aenter__(), timeout=0.8)
            assert not b.is_leader

    async def test_standby_promotes_after_the_holder_releases(self, redis: aioredis.Redis) -> None:
        a = LeaderLease(redis, keys.LEADER_LEASE, ttl_ms=2000, renew_ms=500)
        async with a.hold():
            pass  # clean exit releases immediately rather than waiting out the TTL

        b = LeaderLease(redis, keys.LEADER_LEASE, ttl_ms=2000, renew_ms=500)
        async with asyncio.timeout(2.0), b.hold():
            assert b.is_leader

    async def test_lease_expires_if_the_holder_stops_renewing(self, redis: aioredis.Redis) -> None:
        """Failover is bounded by the TTL even when the holder dies unclean."""
        await redis.set(keys.LEADER_LEASE, "crashed-instance", px=300)
        standby = LeaderLease(redis, keys.LEADER_LEASE, ttl_ms=2000, renew_ms=500)
        async with asyncio.timeout(3.0), standby.hold():
            assert standby.is_leader

    async def test_renew_interval_must_fit_twice_inside_the_ttl(
        self, redis: aioredis.Redis
    ) -> None:
        """Otherwise one dropped packet costs you leadership while perfectly healthy."""
        with pytest.raises(ValueError, match="at least two renewal attempts"):
            LeaderLease(redis, keys.LEADER_LEASE, ttl_ms=1000, renew_ms=900)
