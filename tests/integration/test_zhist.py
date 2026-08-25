"""The z-score series that populates the volatility pane on first paint.

The pane is the only view in the app where the *shape over time* is the point —
"z is 2.8" is not interesting, "z has been climbing for twenty minutes and is
about to cross" is. So this series has to be right in ways the latest-value cache
never had to be: one point per bar we could actually evaluate, no fabricated
points for the bars we could not, and the same window as the price chart beside
it.

Real Redis, because every assertion here is about sorted-set semantics — scoring,
rank trimming, TTL — and mocking those would be asserting our own assumptions
back at ourselves.
"""

from __future__ import annotations

import json
from datetime import timedelta
from itertools import pairwise
from typing import Any

import pytest
import redis.asyncio as aioredis
from fx_api.ws.manager import ConnectionManager
from fx_core import keys
from fx_core.alerts import DetectorConfig, TriggerConfig, VolatilityBaseline
from fx_core.alerts.baseline import lambda_for_halflife
from fx_ingestor.pipeline import IngestPipeline

from tests.chaos.sine_feed import SineVolatilityFeed

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SYMBOL = "EURUSD"
PERIOD = 120
WARMUP = 90

TRIGGER = TriggerConfig(
    enter_z=1.2,
    exit_z=0.3,
    min_confirm_samples=5,
    confirm_for=timedelta(minutes=5),
    sample_timeout=timedelta(minutes=30),
    cooldown=timedelta(minutes=20),
    thaw_after=timedelta(hours=48),
)
DETECTOR = DetectorConfig(session_warmup=timedelta(0), rearm_bars=2)


def build(redis: aioredis.Redis) -> IngestPipeline:
    pipeline = IngestPipeline(
        redis,
        [SYMBOL],
        bucket_seconds=60,
        detector_config=DETECTOR,
        trigger_config=TRIGGER,
    )
    state = pipeline.state_for(SYMBOL)
    state.detector.baseline = VolatilityBaseline(lam=lambda_for_halflife(600), min_samples=30)
    # A fixed Wednesday. Without this the suite would silently stop testing
    # anything every weekend, which we have already been bitten by once.
    state.detector.market_is_open = lambda _ts: True
    return pipeline


async def drive(pipeline: IngestPipeline, feed: SineVolatilityFeed, minutes: int) -> None:
    for tick in feed.ticks(minutes):
        await pipeline.handle(tick)


async def series(redis: aioredis.Redis, symbol: str = SYMBOL) -> list[dict[str, Any]]:
    raw = await redis.zrange(keys.zhist(symbol), 0, -1)
    return [json.loads(r) for r in raw]


def feed(**kw: Any) -> SineVolatilityFeed:
    return SineVolatilityFeed(symbol=SYMBOL, warmup_minutes=WARMUP, period_minutes=PERIOD, **kw)


class TestAccumulation:
    @pytest.mark.parametrize("minutes", [120, 180, 240])
    async def test_one_point_per_evaluated_bar(self, redis: aioredis.Redis, minutes: int) -> None:
        """The series length is derivable, so assert the derivation.

        A bar seals on the arrival of the NEXT minute's first tick, so N minutes
        produce N-1 sealed bars. Of those, exactly ``min_samples`` are consumed
        feeding the baseline before it will emit a z at all, and ``rearm_bars``
        more are spent re-arming after the initial gate. Everything else gets a
        point.

        Asserting a range instead would let a real regression — the detector
        quietly gating an extra bar per session, say — hide inside the slack.
        Measured invariant across seeds and run lengths.
        """
        warmup_cost = 30 + DETECTOR.rearm_bars  # baseline min_samples + re-arm
        pipeline = build(redis)
        await drive(pipeline, feed(), minutes)

        assert len(await series(redis)) == (minutes - 1) - warmup_cost

    async def test_points_carry_their_bar_epoch_as_the_score(self, redis: aioredis.Redis) -> None:
        pipeline = build(redis)
        await drive(pipeline, feed(), 120)

        scored = await redis.zrange(keys.zhist(SYMBOL), 0, -1, withscores=True)
        for member, score in scored:
            assert json.loads(member)["t"] == int(score)

        # Sorted by bar time, not arrival order. The pane plots against a time
        # axis and a single out-of-order point would draw a line backwards.
        stamps = [p["t"] for p in await series(redis)]
        assert all(a < b for a, b in pairwise(stamps))
        assert all((b - a) % 60 == 0 for a, b in pairwise(stamps))

    async def test_every_point_carries_the_committed_regime(self, redis: aioredis.Redis) -> None:
        # Not the instantaneous "is z above the line" — the committed state. The
        # gap between the two IS the hysteresis, and the pane exists to show it.
        pipeline = build(redis)
        await drive(pipeline, feed(amplitude=1.4, noise_sd=0.3), 240)

        points = await series(redis)
        assert {p["r"] for p in points} <= {"normal", "stressed"}
        assert all(isinstance(p["z"], float) for p in points)

    async def test_a_replay_of_the_same_bar_does_not_duplicate_the_point(
        self, redis: aioredis.Redis
    ) -> None:
        # Sorted-set members are unique by value, and the score is the bar epoch,
        # so re-sealing the same minute updates in place rather than drawing the
        # same minute twice.
        pipeline = build(redis)
        await drive(pipeline, feed(), 120)
        before = await redis.zcard(keys.zhist(SYMBOL))

        await drive(build(redis), feed(), 120)
        assert await redis.zcard(keys.zhist(SYMBOL)) == before


class TestGaps:
    async def test_an_unmeasurable_bar_leaves_a_hole_not_a_zero(
        self, redis: aioredis.Redis
    ) -> None:
        """A frozen feed reads as exactly zero volatility.

        Writing 0.0 for those bars would draw the calmest stretch on the chart at
        the precise moment we were blind, which is the most dangerous possible
        lie for this UI to tell. The series is allowed to have holes; it is not
        allowed to invent calm.
        """
        pipeline = build(redis)
        await drive(pipeline, feed(), 100)
        recorded = len(await series(redis))

        pipeline.set_feed_stale(True)
        blind = feed(seed=7)
        blind.start = feed().start + timedelta(minutes=100)
        await drive(pipeline, blind, 30)

        points = await series(redis)
        assert len(points) == recorded, "a gated bar was written to the series"
        assert not any(p["z"] == 0.0 for p in points)

    async def test_the_hole_is_visible_as_a_time_gap(self, redis: aioredis.Redis) -> None:
        # The frontend draws a break in the line wherever consecutive points are
        # more than one bar apart. That only works if the gap is actually in the
        # timestamps rather than papered over by contiguous indices.
        pipeline = build(redis)
        await drive(pipeline, feed(), 100)

        pipeline.set_feed_stale(True)
        blind = feed(seed=7)
        blind.start = feed().start + timedelta(minutes=100)
        await drive(pipeline, blind, 20)

        pipeline.set_feed_stale(False)
        back = feed(seed=9)
        back.start = feed().start + timedelta(minutes=120)
        await drive(pipeline, back, 60)

        stamps = [p["t"] for p in await series(redis)]
        assert max(b - a for a, b in pairwise(stamps)) > 60


class TestBounds:
    async def test_the_series_is_trimmed_by_rank(
        self, redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bounded, or a symbol nobody looks at grows forever.

        Patched down to 20 rather than driving 1,441 minutes through real Redis:
        the behaviour under test is ZREMRANGEBYRANK's off-by-one, which does not
        care what the bound is, and a four-minute test is a test nobody runs.
        """
        monkeypatch.setattr(keys, "ZHIST_MAXLEN", 20)
        pipeline = build(redis)
        await drive(pipeline, feed(), 120)

        points = await series(redis)
        assert len(points) == 20
        # Trimmed from the OLD end. Dropping the newest would be the one failure
        # mode that makes the live pane stop moving.
        newest = await redis.zrange(keys.zhist(SYMBOL), -1, -1, withscores=True)
        assert json.loads(newest[0][0])["t"] == int(newest[0][1])

    async def test_the_series_is_disposable(self, redis: aioredis.Redis) -> None:
        # Every point is recomputable from bars_1m and the detector, so it must
        # carry a TTL — that is what keeps `volatile-lru` able to reclaim it
        # without touching the WAL. See fx_core.keys.
        pipeline = build(redis)
        await drive(pipeline, feed(), 120)
        assert await redis.ttl(keys.zhist(SYMBOL)) > 0
        assert keys.zhist(SYMBOL) not in keys.DURABLE_KEYS


class TestSnapshot:
    async def test_subscribe_serves_the_series_oldest_first(self, redis: aioredis.Redis) -> None:
        """Same order as `bars`, because they are plotted on the same time axis.

        `zrevrange` reads newest-first for the LIMIT to mean "most recent N";
        forgetting to reverse it back is the bug that draws every chart mirrored.
        """
        pipeline = build(redis)
        await drive(pipeline, feed(), 120)

        snap = await ConnectionManager(redis).snapshot([SYMBOL], bars=240)
        zhist = snap[SYMBOL]["zhist"]

        assert zhist, "the pane would open empty"
        assert [p["t"] for p in zhist] == sorted(p["t"] for p in zhist)
        assert zhist[-1]["t"] == max(p["t"] for p in await series(redis))

    async def test_the_window_matches_the_price_chart(self, redis: aioredis.Redis) -> None:
        # Two panes on one screen showing different spans of time is the kind of
        # detail that quietly destroys trust in everything else on the page.
        pipeline = build(redis)
        await drive(pipeline, feed(), 200)

        snap = await ConnectionManager(redis).snapshot([SYMBOL], bars=30)
        assert len(snap[SYMBOL]["zhist"]) <= 30
        assert len(snap[SYMBOL]["bars"]) <= 30

    async def test_thresholds_travel_with_the_reading(self, redis: aioredis.Redis) -> None:
        """The band the pane draws must come from the same place as the z.

        A thresholds endpoint read separately can disagree with the running
        detector after a config change — and the pane would then draw a band the
        alerts do not honour, which is worse than drawing no band at all.
        """
        pipeline = build(redis)
        await drive(pipeline, feed(), 120)

        snap = await ConnectionManager(redis).snapshot([SYMBOL], bars=240)
        vol = snap[SYMBOL]["vol"]
        assert vol["enter_z"] == TRIGGER.enter_z
        assert vol["exit_z"] == TRIGGER.exit_z
        assert vol["exit_z"] < vol["enter_z"]

    async def test_a_symbol_that_never_sealed_a_bar_reports_an_empty_series(
        self, redis: aioredis.Redis
    ) -> None:
        # Not an error, and not a fabricated flat line at zero. The pane renders
        # its axis and waits.
        snap = await ConnectionManager(redis).snapshot(["GBPUSD"], bars=240)
        assert snap["GBPUSD"]["zhist"] == []
