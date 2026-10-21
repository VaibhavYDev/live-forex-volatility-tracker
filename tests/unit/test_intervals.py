"""Timeframe roll-up.

OHLC aggregation is a place where a wrong answer looks right. Averaging opens,
or taking the last open instead of the first, produces a chart that is
well-formed, plausible, and lying about every candle on it. So the ordering
rules get pinned explicitly rather than checked by eye.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fx_core.intervals import INTERVALS, ORDER, base_count, bucket_start, fold


def bar(
    t: int, o: float, h: float, low: float, c: float, n: int = 1, src: str = "replay"
) -> dict[str, object]:
    return {"t": t, "o": o, "h": h, "l": low, "c": c, "n": n, "src": src}


def ts(spec: str) -> int:
    return int(datetime.fromisoformat(spec).replace(tzinfo=UTC).timestamp())


class TestBucketing:
    @pytest.mark.parametrize("code", [c for c, i in INTERVALS.items() if i.seconds])
    def test_fixed_intervals_align_to_midnight(self, code: str) -> None:
        # Every fixed interval divides 86_400, so a day boundary must also be a
        # bucket boundary. An interval that failed this would drift the whole
        # chart a little further from the clock each day.
        midnight = ts("2026-03-11T00:00:00")
        assert bucket_start(midnight, code) == midnight

    def test_four_hour_buckets_land_on_the_expected_edge(self) -> None:
        assert bucket_start(ts("2026-03-11T13:47:31"), "4h") == ts("2026-03-11T12:00:00")

    def test_a_week_starts_on_monday(self) -> None:
        # Wednesday resolves back to Monday, not to "seven days ago".
        assert bucket_start(ts("2026-03-11T13:47:31"), "1w") == ts("2026-03-09T00:00:00")

    def test_a_monday_is_its_own_week(self) -> None:
        monday = ts("2026-03-09T00:00:00")
        assert bucket_start(monday, "1w") == monday

    def test_a_week_bucket_crosses_a_month_boundary(self) -> None:
        # 1 March 2026 is a Sunday, so it belongs to the week beginning 23 Feb.
        # Fixed-offset arithmetic gets this wrong.
        assert bucket_start(ts("2026-03-01T09:00:00"), "1w") == ts("2026-02-23T00:00:00")

    def test_a_month_starts_on_the_first(self) -> None:
        assert bucket_start(ts("2026-03-11T13:47:31"), "1M") == ts("2026-03-01T00:00:00")

    def test_months_are_not_thirty_days(self) -> None:
        # February is the test that kills fixed-length month arithmetic.
        assert bucket_start(ts("2026-02-28T23:59:59"), "1M") == ts("2026-02-01T00:00:00")
        assert bucket_start(ts("2026-03-01T00:00:00"), "1M") == ts("2026-03-01T00:00:00")


class TestFold:
    def test_open_is_the_first_and_close_is_the_last(self) -> None:
        """The assertion that catches an averaged or reversed roll-up."""
        base = [
            bar(ts("2026-03-11T10:00:00"), o=1.10, h=1.12, low=1.09, c=1.11),
            bar(ts("2026-03-11T10:01:00"), o=1.11, h=1.15, low=1.10, c=1.13),
            bar(ts("2026-03-11T10:02:00"), o=1.13, h=1.14, low=1.05, c=1.06),
        ]
        [out] = fold(base, "5m")

        assert out["o"] == 1.10, "open must come from the first bar"
        assert out["c"] == 1.06, "close must come from the last bar"
        assert out["h"] == 1.15
        assert out["l"] == 1.05

    def test_tick_counts_sum(self) -> None:
        base = [
            bar(ts("2026-03-11T10:00:00"), 1, 1, 1, 1, n=7),
            bar(ts("2026-03-11T10:01:00"), 1, 1, 1, 1, n=11),
        ]
        assert fold(base, "5m")[0]["n"] == 18

    def test_bars_split_across_buckets(self) -> None:
        base = [
            bar(ts("2026-03-11T10:04:00"), o=1.0, h=1.0, low=1.0, c=1.0),
            bar(ts("2026-03-11T10:05:00"), o=2.0, h=2.0, low=2.0, c=2.0),
        ]
        out = fold(base, "5m")

        assert len(out) == 2, "10:04 and 10:05 are different 5m buckets"
        assert [b["t"] for b in out] == [ts("2026-03-11T10:00:00"), ts("2026-03-11T10:05:00")]

    def test_unsorted_input_still_folds_correctly(self) -> None:
        # Redis ZREVRANGE hands back newest-first; a caller that forgets to
        # reverse would otherwise silently invert every open and close.
        base = [
            bar(ts("2026-03-11T10:02:00"), o=1.13, h=1.14, low=1.05, c=1.06),
            bar(ts("2026-03-11T10:00:00"), o=1.10, h=1.12, low=1.09, c=1.11),
        ]
        [out] = fold(base, "5m")
        assert (out["o"], out["c"]) == (1.10, 1.06)

    def test_a_partial_trailing_bucket_is_kept(self) -> None:
        # The hour in progress is real and the chart should draw it forming.
        # Dropping it makes the most recent price permanently up to an hour old.
        base = [bar(ts("2026-03-11T10:00:00"), 1, 1, 1, 1)]
        assert len(fold(base, "1h")) == 1

    def test_one_minute_is_a_passthrough(self) -> None:
        base = [bar(ts("2026-03-11T10:00:00"), 1.1, 1.2, 1.0, 1.15, n=3)]
        [out] = fold(base, "1m")
        assert (out["t"], out["o"], out["h"], out["l"], out["c"], out["n"]) == (
            base[0]["t"],
            1.1,
            1.2,
            1.0,
            1.15,
            3,
        )

    def test_empty_input(self) -> None:
        assert fold([], "1h") == []

    def test_an_unknown_interval_is_rejected(self) -> None:
        # Reaches this from a query string, so it must fail loudly rather than
        # silently returning unaggregated bars.
        with pytest.raises(ValueError, match="unknown interval"):
            fold([bar(0, 1, 1, 1, 1)], "3y")

    def test_folding_does_not_mutate_the_input(self) -> None:
        # The caller's bars come from a cache; mutating them corrupts the next
        # request for a different interval.
        base = [
            bar(ts("2026-03-11T10:00:00"), o=1.10, h=1.12, low=1.09, c=1.11, n=5),
            bar(ts("2026-03-11T10:01:00"), o=1.11, h=1.15, low=1.10, c=1.13, n=5),
        ]
        fold(base, "1h")
        assert base[0]["n"] == 5 and base[0]["h"] == 1.12


class TestProvenance:
    """A folded candle must not claim a single source it does not have."""

    def test_a_bucket_from_one_source_keeps_it(self) -> None:
        base = [
            bar(ts("2026-03-11T10:00:00"), 1, 1, 1, 1, src="stream"),
            bar(ts("2026-03-11T10:01:00"), 1, 1, 1, 1, src="stream"),
        ]
        assert fold(base, "5m")[0]["src"] == "stream"

    def test_history_turning_live_inside_a_bucket_is_mixed(self) -> None:
        # The 15m candle the stream joined halfway through used to be labelled
        # "backfill" because its first minute was, and the chart called the
        # whole candle provider history.
        base = [
            bar(ts("2026-03-11T10:00:00"), 1, 1, 1, 1, n=0, src="backfill"),
            bar(ts("2026-03-11T10:07:00"), 1, 1, 1, 1, n=9, src="stream"),
        ]
        [out] = fold(base, "15m")
        assert out["src"] == "mixed"
        assert out["n"] == 9, "history contributes no ticks; none are invented"

    def test_mixed_does_not_leak_into_the_next_bucket(self) -> None:
        base = [
            bar(ts("2026-03-11T10:00:00"), 1, 1, 1, 1, src="backfill"),
            bar(ts("2026-03-11T10:04:00"), 1, 1, 1, 1, src="stream"),
            bar(ts("2026-03-11T10:05:00"), 1, 1, 1, 1, src="stream"),
        ]
        assert [b["src"] for b in fold(base, "5m")] == ["mixed", "stream"]

    @pytest.mark.parametrize("reverse", [False, True])
    def test_history_and_live_for_the_same_minute_fold_in_a_fixed_order(
        self, reverse: bool
    ) -> None:
        # The provider's bar covers the minute from its first second; ours may
        # have joined mid-minute. Open from the provider, close from the stream,
        # extremes from both - whatever order the cache returned them in.
        minute = ts("2026-03-11T10:03:00")
        base = [
            bar(minute, o=1.10, h=1.20, low=1.05, c=1.15, n=0, src="backfill"),
            bar(minute, o=1.14, h=1.18, low=1.01, c=1.16, n=4, src="stream"),
        ]
        if reverse:
            base.reverse()
        [out] = fold(base, "5m")
        assert (out["o"], out["h"], out["l"], out["c"], out["n"], out["src"]) == (
            1.10,
            1.20,
            1.01,
            1.16,
            4,
            "mixed",
        )


def test_every_ordered_code_is_a_real_interval() -> None:
    # ORDER drives the UI buttons; a typo would render a control that 422s.
    assert set(ORDER) <= set(INTERVALS)


def test_the_dashboard_offers_exactly_the_requested_timeframes() -> None:
    assert ORDER == ("1m", "3m", "5m", "10m", "15m", "30m", "1d")


PAIRS = {"EURUSD": 1.0842, "GBPUSD": 1.2712, "USDJPY": 147.236, "AUDUSD": 0.6617, "USDCHF": 0.8731}


class TestShortIntervals:
    """3m and 10m fold the stored minutes exactly as 5m does."""

    @pytest.mark.parametrize(("code", "edge"), [("3m", "13:45:00"), ("10m", "13:40:00")])
    def test_buckets_land_on_utc_edges(self, code: str, edge: str) -> None:
        assert bucket_start(ts("2026-03-11T13:47:31"), code) == ts(f"2026-03-11T{edge}")

    @pytest.mark.parametrize("symbol", PAIRS)
    @pytest.mark.parametrize(("code", "candles"), [("3m", 4), ("10m", 2)])
    def test_every_pair_folds_without_inventing_minutes(
        self, symbol: str, code: str, candles: int
    ) -> None:
        # 13:40-13:47 traded, 13:48-13:53 nothing did, 13:54-13:55 is still forming.
        px, step = PAIRS[symbol], PAIRS[symbol] * 1e-4
        base = [
            bar(
                ts(f"2026-03-11T13:{m}:00"),
                o=px + i * step,
                h=px + (i + 2) * step,
                low=px + (i - 1) * step,
                c=px + (i + 1) * step,
                n=3,
            )
            for i, m in enumerate([*range(40, 48), 54, 55])
        ]
        seconds = INTERVALS[code].seconds
        assert seconds is not None

        out = fold(base, code)

        assert len(out) == candles
        for candle in out:
            members = [b for b in base if int(b["t"]) - int(b["t"]) % seconds == candle["t"]]
            assert candle["t"] % seconds == 0
            assert (candle["o"], candle["c"]) == (members[0]["o"], members[-1]["c"])
            assert candle["h"] == max(float(b["h"]) for b in members)  # type: ignore[arg-type]
            assert candle["l"] == min(float(b["l"]) for b in members)  # type: ignore[arg-type]
            assert candle["n"] == 3 * len(members)


class TestBaseCount:
    def test_one_minute_needs_one_base_bar_each(self) -> None:
        assert base_count("1m", 100) == 101  # +1 bucket of slack

    def test_an_hour_needs_sixty_minutes_each(self) -> None:
        assert base_count("1h", 100) == 6_060

    def test_four_hours_reads_from_minutes(self) -> None:
        # 4h folds from the 1m series, not the daily one: 240 minutes a candle.
        assert INTERVALS["4h"].base == "1m"
        assert base_count("4h", 10) == 2_640

    def test_a_day_reads_from_the_daily_series(self) -> None:
        # Folding 1d from minutes would need 1_440 base bars per candle and cap
        # the chart at the 30-day minute window.
        assert INTERVALS["1d"].base == "1d"
        assert base_count("1d", 200) == 201

    def test_a_week_reads_seven_days_per_candle(self) -> None:
        assert base_count("1w", 100) == 707

    def test_a_month_over_fetches_rather_than_coming_up_short(self) -> None:
        # 31, not 30: a 30-day assumption returns a short chart every February.
        assert base_count("1M", 12) == 403

    def test_short_intervals_read_their_minutes(self) -> None:
        assert base_count("3m", 100) == 303
        assert base_count("10m", 100) == 1_010

    def test_every_interval_can_be_costed(self) -> None:
        for code in INTERVALS:
            assert base_count(code, 200) > 0
