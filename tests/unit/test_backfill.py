"""Synthetic history.

The bars this produces are drawn on a chart and fed to the same volatility
estimators as observed data. Two classes of defect matter:

  * Malformed OHLC (high below close, zero-height bars) — breaks Parkinson and
    Garman-Klass, which divide by the range.
  * Non-determinism — two ingestor replicas would backfill different pasts, so
    a standby promoting mid-session redraws the chart under the viewer.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from itertools import pairwise

import pytest
from fx_core.backfill import SECONDS_PER_TRADING_YEAR, synth_bars

NOW = int(datetime(2026, 3, 11, 13, 47, 31, tzinfo=UTC).timestamp())

KW = {
    "end_epoch": NOW,
    "count": 500,
    "seconds": 60,
    "price": 1.0850,
    "annual_vol": 0.070,
    "pip": 0.0001,
    "seed": 42,
}


@pytest.fixture(scope="module")
def bars() -> list[dict]:
    return synth_bars(**KW)  # type: ignore[arg-type]


class TestShape:
    def test_it_returns_the_requested_count(self, bars: list[dict]) -> None:
        assert len(bars) == 500

    def test_bars_are_contiguous_and_ascending(self, bars: list[dict]) -> None:
        # A gap would draw as a hole in the chart; a duplicate would make the
        # roll-up fold two bars into one bucket and halve the candle count.
        times = [b["t"] for b in bars]
        assert times == sorted(times)
        assert all(b - a == 60 for a, b in pairwise(times))

    def test_the_last_bar_is_aligned_to_its_bucket(self, bars: list[dict]) -> None:
        assert bars[-1]["t"] % 60 == 0
        assert bars[-1]["t"] <= NOW

    def test_it_ends_where_the_live_feed_starts(self, bars: list[dict]) -> None:
        # The join. A mismatch here draws a gap candle at exactly the moment the
        # demo begins streaming, which is the first thing anyone notices.
        assert bars[-1]["c"] == pytest.approx(1.0850, abs=1e-6)

    def test_empty_when_asked_for_nothing(self) -> None:
        assert synth_bars(**{**KW, "count": 0}) == []  # type: ignore[arg-type]


class TestOhlcIsWellFormed:
    def test_high_is_the_highest_and_low_the_lowest(self, bars: list[dict]) -> None:
        for b in bars:
            assert b["h"] >= max(b["o"], b["c"]), b
            assert b["l"] <= min(b["o"], b["c"]), b
            assert b["h"] >= b["l"]

    def test_no_bar_has_zero_range(self, bars: list[dict]) -> None:
        # Parkinson and Garman-Klass take log(high/low). A zero-range bar makes
        # that term zero and drags the estimate toward nonsense.
        assert all(b["h"] > b["l"] for b in bars)

    def test_prices_stay_positive(self, bars: list[dict]) -> None:
        # GBM cannot go negative; if it does, the walk was implemented additively.
        assert all(b["l"] > 0 for b in bars)

    def test_tick_counts_are_non_zero(self, bars: list[dict]) -> None:
        assert all(b["n"] > 0 for b in bars)

    def test_every_bar_is_labelled_as_backfill(self, bars: list[dict]) -> None:
        # The honesty guarantee: nothing downstream may mistake these for
        # observed ticks.
        assert {b["src"] for b in bars} == {"backfill"}


class TestDeterminism:
    def test_same_inputs_give_identical_bars(self) -> None:
        assert synth_bars(**KW) == synth_bars(**KW)  # type: ignore[arg-type]

    def test_a_different_symbol_price_gives_a_different_series(self) -> None:
        other = synth_bars(**{**KW, "price": 147.20})  # type: ignore[arg-type]
        assert [b["c"] for b in other] != [b["c"] for b in synth_bars(**KW)]  # type: ignore[arg-type]

    def test_a_different_interval_gives_a_different_series(self) -> None:
        # Otherwise the daily series would be a rescaled copy of the minute one
        # and the two would visibly rhyme on screen.
        daily = synth_bars(**{**KW, "seconds": 86_400})  # type: ignore[arg-type]
        assert [b["c"] for b in daily] != [b["c"] for b in synth_bars(**KW)]  # type: ignore[arg-type]


def test_realised_volatility_lands_near_the_stated_figure() -> None:
    """The claim the chart makes beside itself must survive measurement.

    The panel prints "EWMA sigma, annualised" next to these bars. If the
    generator's realised vol were off by an order of magnitude, the number and
    the picture would contradict each other.
    """
    bars = synth_bars(**{**KW, "count": 20_000})  # type: ignore[arg-type]
    rets = [math.log(b["c"] / a["c"]) for a, b in pairwise(bars)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    annualised = math.sqrt(var * SECONDS_PER_TRADING_YEAR / 60)

    # Sampling error on 20k draws is a couple of percent; the band is wide
    # enough not to flake and tight enough to catch a sqrt(dt) mistake, which
    # would be wrong by ~250x.
    assert 0.060 < annualised < 0.081, annualised


def test_volatility_is_relative_to_price_not_absolute() -> None:
    """Catches an additive walk masquerading as a multiplicative one.

    Mutation testing found the positivity check above passes for an ADDITIVE
    walk too: sigma per minute is ~1.2e-4, so 500 additive steps from 1.085
    never reach zero and the test sees nothing wrong.

    The property that actually separates the two is scale invariance. Under GBM
    a 147-yen pair and a 1.08-dollar pair have the SAME relative volatility;
    under an additive walk the yen pair's would be ~135x smaller, because a
    fixed absolute step is a much smaller fraction of a larger price.
    """
    yen = synth_bars(**{**KW, "count": 20_000, "price": 147.20})  # type: ignore[arg-type]
    rets = [math.log(b["c"] / a["c"]) for a, b in pairwise(yen)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    annualised = math.sqrt(var * SECONDS_PER_TRADING_YEAR / 60)

    assert 0.060 < annualised < 0.081, (
        f"{annualised:.5f} - relative vol must not depend on the price level"
    )


def test_bars_actually_have_wicks(bars: list[dict]) -> None:
    """Also found by mutation testing: 'range > 0' does not imply a wick.

    Dropping the wick entirely leaves h == max(o, c), which still satisfies
    h > l on any bar that moved at all. But a candle whose high is exactly its
    body renders as a flat-topped block, and Parkinson - which reads the
    high-low range - systematically understates volatility on such bars.
    """
    upper = sum(1 for b in bars if b["h"] > max(b["o"], b["c"]))
    lower = sum(1 for b in bars if b["l"] < min(b["o"], b["c"]))

    assert upper > len(bars) * 0.9, f"only {upper}/{len(bars)} bars have an upper wick"
    assert lower > len(bars) * 0.9, f"only {lower}/{len(bars)} bars have a lower wick"
