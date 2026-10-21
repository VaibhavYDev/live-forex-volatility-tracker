"""The minute a browser draws live, as the ingestor aggregates it.

The chart's forming candle used to be rebuilt in the browser from the quotes it
happened to receive. The API conflates ticks for a slow reader and the browser
conflates again per painted frame, so the high or low of any tick that was
superseded before it arrived never reached the candle - on every timeframe that
drew one, which was only 1m.

These pin what replaces it: every tick frame carries the minute so far, cumulative
over every tick the ingestor saw, so any one frame is enough; the frame that seals
a minute carries the bar the history set stores; the snapshot carries the minute
in progress. All checked against an aggregation done here, tick by tick.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis.asyncio as aioredis
from fx_api.ws.manager import ConnectionManager
from fx_core import keys
from fx_core.models import Tick
from fx_ingestor.pipeline import IngestPipeline

pytestmark = pytest.mark.integration

SYMBOL = "EURUSD"
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"]
MINUTE = datetime(2026, 8, 19, 12, 7, tzinfo=UTC)

# Up, down past the open, up past the first high, back inside: the high and low
# are set by ticks that are not the last, which is what conflation used to lose.
PRICES = [1.08420, 1.08455, 1.08390, 1.08470, 1.08401, 1.08433]


def _tick(symbol: str, price: float, ts: datetime, seq: int) -> Tick:
    return Tick(
        symbol=symbol,
        bid=price - 0.00005,
        ask=price + 0.00005,
        ts_event=ts,
        ts_ingest=ts,
        seq=seq,
    )


def _aggregate(mids: list[float], t: int) -> dict[str, Any]:
    """The minute, worked out from the ticks themselves rather than by the pipeline."""
    return {
        "t": t,
        "o": mids[0],
        "h": max(mids),
        "l": min(mids),
        "c": mids[-1],
        "n": len(mids),
    }


async def _frames(pubsub: aioredis.client.PubSub, count: int) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for _ in range(200):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05)
        if message:
            out.append((str(message["channel"]), json.loads(message["data"])))
            if len(out) == count:
                break
    return out


async def _listen(redis: aioredis.Redis, *channels: str) -> aioredis.client.PubSub:
    pubsub = redis.pubsub()
    await pubsub.subscribe(*channels)
    await asyncio.sleep(0.05)
    return pubsub


async def _feed(
    pipeline: IngestPipeline, symbol: str, start: datetime, prices: list[float]
) -> list[float]:
    """One tick per price, a few seconds apart; the mids the pipeline aggregates."""
    mids = []
    for i, price in enumerate(prices):
        tick = _tick(symbol, price, start + timedelta(seconds=3 * i + 1), i)
        mids.append(tick.mid)
        await pipeline.handle(tick)
    return mids


class TestTickFrames:
    async def test_every_frame_carries_the_minute_so_far(self, redis: aioredis.Redis) -> None:
        pubsub = await _listen(redis, keys.channel_tick(SYMBOL))
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)

        mids = await _feed(pipeline, SYMBOL, MINUTE, PRICES)
        frames = await _frames(pubsub, len(PRICES))
        await pubsub.aclose()

        assert len(frames) == len(PRICES)
        t = int(MINUTE.timestamp())
        for i, (_, frame) in enumerate(frames):
            assert frame["bar"] == _aggregate(mids[: i + 1], t), f"after tick {i}"

    async def test_the_last_frame_alone_holds_every_extreme(self, redis: aioredis.Redis) -> None:
        # A reader that only ever gets the final frame of the minute - the
        # conflated case - still has the high and low set three ticks earlier.
        pubsub = await _listen(redis, keys.channel_tick(SYMBOL))
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)

        mids = await _feed(pipeline, SYMBOL, MINUTE, PRICES)
        *_, (_, last) = await _frames(pubsub, len(PRICES))
        await pubsub.aclose()

        assert (last["bar"]["h"], last["bar"]["l"]) == (max(mids), min(mids))
        assert last["m"] != last["bar"]["h"], "the fixture must not end on its own high"

    async def test_the_quote_fields_are_unchanged(self, redis: aioredis.Redis) -> None:
        # The bar is an addition: a browser that predates it reads the same quote.
        pubsub = await _listen(redis, keys.channel_tick(SYMBOL))
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)
        tick = _tick(SYMBOL, 1.0842, MINUTE, 1)
        await pipeline.handle(tick)
        [(_, frame)] = await _frames(pubsub, 1)
        await pubsub.aclose()

        assert set(frame) == {"s", "b", "a", "m", "t", "bar"}
        assert (frame["s"], frame["b"], frame["a"], frame["m"]) == (
            SYMBOL,
            tick.bid,
            tick.ask,
            tick.mid,
        )
        assert frame["t"] == MINUTE.isoformat()

    async def test_the_frame_matches_the_stored_bucket(self, redis: aioredis.Redis) -> None:
        pubsub = await _listen(redis, keys.channel_tick(SYMBOL))
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)
        await _feed(pipeline, SYMBOL, MINUTE, PRICES)
        *_, (_, last) = await _frames(pubsub, len(PRICES))
        await pubsub.aclose()

        stored = await redis.hgetall(keys.bar_bucket(SYMBOL, int(MINUTE.timestamp())))
        assert last["bar"] == {
            "t": int(MINUTE.timestamp()),
            "o": float(stored["o"]),
            "h": float(stored["h"]),
            "l": float(stored["l"]),
            "c": float(stored["c"]),
            "n": int(stored["n"]),
        }


class TestSealedFrames:
    async def test_the_vol_frame_carries_the_bar_history_stored(
        self, redis: aioredis.Redis
    ) -> None:
        pubsub = await _listen(redis, keys.channel_vol(SYMBOL))
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)

        mids = await _feed(pipeline, SYMBOL, MINUTE, PRICES)
        # First tick of the next minute seals this one.
        await pipeline.handle(_tick(SYMBOL, 1.0850, MINUTE + timedelta(seconds=60), 99))
        [(_, frame)] = await _frames(pubsub, 1)
        await pubsub.aclose()

        [stored] = [json.loads(m) for m in await redis.zrange(keys.history(SYMBOL), 0, -1)]
        assert frame["bar"] == stored
        assert frame["bar"] == {**_aggregate(mids, int(MINUTE.timestamp())), "src": "stream"}

    async def test_the_stored_reading_does_not_keep_the_bar(self, redis: aioredis.Redis) -> None:
        # The bar belongs to the frame. The snapshot already sends it with the
        # history; storing it again in every reading would send it twice.
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)
        await _feed(pipeline, SYMBOL, MINUTE, PRICES)
        await pipeline.handle(_tick(SYMBOL, 1.0850, MINUTE + timedelta(seconds=60), 99))

        reading = await redis.hgetall(keys.vol_state(SYMBOL, "1h"))
        assert reading, "the minute sealed, so a reading was stored"
        assert "bar" not in reading

    async def test_the_sealing_tick_opens_the_next_minute_before_the_seal_is_sent(
        self, redis: aioredis.Redis
    ) -> None:
        # One transaction publishes both. A browser sees the new minute begin,
        # then the old one's final bar; either order must leave it consistent,
        # and this is the order it gets.
        pubsub = await _listen(redis, keys.channel_tick(SYMBOL), keys.channel_vol(SYMBOL))
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)
        await _feed(pipeline, SYMBOL, MINUTE, PRICES)
        await _frames(pubsub, len(PRICES))

        nxt = _tick(SYMBOL, 1.0850, MINUTE + timedelta(seconds=60), 99)
        await pipeline.handle(nxt)
        (first_ch, first), (second_ch, second) = await _frames(pubsub, 2)
        await pubsub.aclose()

        assert first_ch == keys.channel_tick(SYMBOL)
        assert first["bar"] == _aggregate([nxt.mid], int(MINUTE.timestamp()) + 60)
        assert second_ch == keys.channel_vol(SYMBOL)
        assert second["bar"]["t"] == int(MINUTE.timestamp())

    async def test_a_quiet_minute_is_skipped_not_filled(self, redis: aioredis.Redis) -> None:
        # 12:07 traded, 12:08 did not, 12:09 did. The seal is 12:07's bar and the
        # next forming bar is 12:09's: nothing stands in for 12:08.
        pubsub = await _listen(redis, keys.channel_tick(SYMBOL), keys.channel_vol(SYMBOL))
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)
        await _feed(pipeline, SYMBOL, MINUTE, PRICES)
        await _frames(pubsub, len(PRICES))

        await pipeline.handle(_tick(SYMBOL, 1.0850, MINUTE + timedelta(seconds=125), 99))
        frames = dict(await _frames(pubsub, 2))
        await pubsub.aclose()

        assert frames[keys.channel_vol(SYMBOL)]["bar"]["t"] == int(MINUTE.timestamp())
        assert frames[keys.channel_tick(SYMBOL)]["bar"]["t"] == int(MINUTE.timestamp()) + 120
        stamps = [json.loads(m)["t"] for m in await redis.zrange(keys.history(SYMBOL), 0, -1)]
        assert stamps == [int(MINUTE.timestamp())]

    @pytest.mark.parametrize("symbol", SYMBOLS)
    async def test_every_pair_publishes_its_own_minutes(
        self, redis: aioredis.Redis, symbol: str
    ) -> None:
        pubsub = await _listen(redis, keys.channel_vol(symbol))
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)
        scale = 150.0 if symbol == "USDJPY" else 1.0
        prices = [p * scale for p in PRICES]
        mids = await _feed(pipeline, symbol, MINUTE, prices)
        await pipeline.handle(_tick(symbol, prices[0], MINUTE + timedelta(seconds=60), 99))
        [(channel, frame)] = await _frames(pubsub, 1)
        await pubsub.aclose()

        assert channel == keys.channel_vol(symbol)
        assert frame["s"] == symbol
        assert frame["bar"] == {**_aggregate(mids, int(MINUTE.timestamp())), "src": "stream"}


class TestSnapshotForming:
    async def test_the_snapshot_carries_the_minute_in_progress(self, redis: aioredis.Redis) -> None:
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)
        await _feed(pipeline, SYMBOL, MINUTE - timedelta(minutes=1), PRICES[:3])
        mids = await _feed(pipeline, SYMBOL, MINUTE, PRICES)

        snap = (await ConnectionManager(redis).snapshot([SYMBOL]))[SYMBOL]

        assert snap["forming"] == _aggregate(mids, int(MINUTE.timestamp()))
        # Sealed minutes and the forming one never share a timestamp.
        assert [b["t"] for b in snap["bars"]] == [int(MINUTE.timestamp()) - 60]

    async def test_nothing_is_made_up_when_no_minute_is_held(self, redis: aioredis.Redis) -> None:
        # The bucket expires an hour after its last tick - every weekend. The
        # quote outlives it; a candle drawn from the quote alone would claim one
        # tick and no range.
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)
        await _feed(pipeline, SYMBOL, MINUTE, PRICES)
        await redis.delete(keys.bar_bucket(SYMBOL, int(MINUTE.timestamp())))

        snap = await ConnectionManager(redis).snapshot([SYMBOL, "GBPUSD"])

        assert snap[SYMBOL]["quote"], "the quote is still there"
        assert snap[SYMBOL]["forming"] is None
        assert snap["GBPUSD"]["forming"] is None, "no quote, no minute"

    async def test_a_minute_that_sealed_meanwhile_reads_as_sealed(
        self, redis: aioredis.Redis
    ) -> None:
        # The quote says 12:07; by the time its bucket is read the minute has
        # sealed. The bucket is the minute as it sealed - never a partial one.
        pipeline = IngestPipeline(redis, SYMBOLS, bucket_seconds=60)
        mids = await _feed(pipeline, SYMBOL, MINUTE, PRICES)
        manager = ConnectionManager(redis)
        quote = await redis.hgetall(keys.quote(SYMBOL))
        await pipeline.handle(_tick(SYMBOL, 1.0850, MINUTE + timedelta(seconds=60), 99))

        forming = await manager._forming([SYMBOL], [quote])

        assert forming[SYMBOL] == _aggregate(mids, int(MINUTE.timestamp()))
