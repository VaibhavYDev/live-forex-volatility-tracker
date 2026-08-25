"""Volatility maths, checked against independently-computed reference values.

The reference values come from ``statistics`` and hand-written textbook formulas
in the test itself, NOT from the implementation. A test that reimplements the
code it tests proves only that you typed it twice.
"""

from __future__ import annotations

import math
import random
import statistics
from datetime import UTC, datetime, timedelta

import pytest
from fx_core.models import Bar
from fx_core.volatility import (
    EwmaVariance,
    Welford,
    close_to_close,
    garman_klass,
    parkinson,
    rogers_satchell,
    yang_zhang,
)
from fx_core.volatility.ewma import lambda_for_halflife


def _bars(closes: list[float], spread: float = 0.001) -> list[Bar]:
    t0 = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(
            Bar(
                symbol="EURUSD",
                bucket=t0 + timedelta(minutes=i),
                open=o,
                high=max(o, c) * (1 + spread),
                low=min(o, c) * (1 - spread),
                close=c,
                tick_count=10,
                sum_ret=0.0,
                sum_ret_sq=0.0,
            )
        )
    return out


class TestWelford:
    def test_matches_statistics_variance(self) -> None:
        rng = random.Random(1)
        xs = [rng.gauss(0, 1e-5) for _ in range(5000)]
        acc = Welford()
        acc.update_many(xs)
        assert acc.variance == pytest.approx(statistics.variance(xs), rel=1e-12)
        assert acc.mean == pytest.approx(statistics.fmean(xs), rel=1e-12)

    def test_beats_naive_sum_of_squares_on_fx_scale_data(self) -> None:
        """The whole reason Welford is here rather than the one-liner.

        FX prices are ~1.08 with ~1e-5 variation, so the naive formula subtracts
        two nearly-equal large numbers and loses most of its significant digits.
        """
        rng = random.Random(7)
        xs = [1.0842 + rng.gauss(0, 1e-5) for _ in range(200_000)]
        exact = statistics.variance(xs)

        acc = Welford()
        acc.update_many(xs)
        welford_err = abs(acc.variance - exact) / exact

        n = len(xs)
        total, total_sq = sum(xs), sum(x * x for x in xs)
        naive = (total_sq - total * total / n) / (n - 1)
        naive_err = abs(naive - exact) / exact

        assert welford_err < 1e-9
        # Not merely better - better by many orders of magnitude.
        assert naive_err > welford_err * 1e6

    def test_merge_equals_single_pass(self) -> None:
        """Chan's parallel combination: this is what makes windowing O(bars)."""
        rng = random.Random(3)
        xs = [rng.gauss(0, 1e-4) for _ in range(4000)]
        whole = Welford()
        whole.update_many(xs)

        left, right = Welford(), Welford()
        left.update_many(xs[:1234])
        right.update_many(xs[1234:])

        assert left.merge(right).variance == pytest.approx(whole.variance, rel=1e-12)

    def test_merge_with_empty_is_identity(self) -> None:
        acc = Welford()
        acc.update_many([1.0, 2.0, 3.0])
        assert acc.merge(Welford()).variance == pytest.approx(acc.variance)
        assert Welford().merge(acc).variance == pytest.approx(acc.variance)

    def test_fewer_than_two_samples_has_no_variance(self) -> None:
        acc = Welford()
        assert acc.variance == 0.0
        acc.update(1.0)
        assert acc.variance == 0.0


class TestEwma:
    def test_halflife_round_trip(self) -> None:
        lam = lambda_for_halflife(30)
        assert lam**30 == pytest.approx(0.5)

    def test_converges_to_true_variance(self) -> None:
        rng = random.Random(11)
        sigma = 2e-4
        ewma = EwmaVariance(lam=0.995)
        for _ in range(50_000):
            ewma.update(rng.gauss(0, sigma))
        assert ewma.sigma == pytest.approx(sigma, rel=0.1)

    def test_rejects_invalid_lambda(self) -> None:
        for bad in (0.0, 1.0, -0.5, 1.5):
            with pytest.raises(ValueError, match="lam must be"):
                EwmaVariance(lam=bad)

    def test_nan_cannot_poison_the_recursion(self) -> None:
        """There is no window for a NaN to age out of - it would be permanent."""
        ewma = EwmaVariance(lam=0.94)
        ewma.update(1e-4)
        before = ewma.variance
        ewma.update(float("nan"))
        ewma.update(float("inf"))
        assert ewma.variance == before
        assert math.isfinite(ewma.sigma)

    def test_warmed_up_gates_early_readings(self) -> None:
        ewma = EwmaVariance(lam=0.9)  # effective memory 10
        ewma.update(1e-4)
        assert not ewma.warmed_up
        for _ in range(20):
            ewma.update(1e-4)
        assert ewma.warmed_up


class TestRangeEstimators:
    def test_parkinson_matches_textbook_formula(self) -> None:
        bars = _bars([1.10, 1.11, 1.09, 1.12, 1.10])
        expected = math.sqrt(
            sum(math.log(b.high / b.low) ** 2 for b in bars) / (4 * math.log(2) * len(bars))
        )
        assert parkinson(bars) == pytest.approx(expected)

    def test_garman_klass_matches_textbook_formula(self) -> None:
        bars = _bars([1.10, 1.11, 1.09, 1.12, 1.10])
        expected = math.sqrt(
            sum(
                0.5 * math.log(b.high / b.low) ** 2
                - (2 * math.log(2) - 1) * math.log(b.close / b.open) ** 2
                for b in bars
            )
            / len(bars)
        )
        assert garman_klass(bars) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "fn", [close_to_close, parkinson, garman_klass, rogers_satchell, yang_zhang]
    )
    def test_flat_market_has_near_zero_volatility(self, fn) -> None:  # type: ignore[no-untyped-def]
        assert fn(_bars([1.10] * 20, spread=0.0)) == pytest.approx(0.0, abs=1e-12)

    @pytest.mark.parametrize(
        "fn", [close_to_close, parkinson, garman_klass, rogers_satchell, yang_zhang]
    )
    def test_never_negative_and_never_nan(self, fn) -> None:  # type: ignore[no-untyped-def]
        rng = random.Random(5)
        closes = [1.10]
        for _ in range(200):
            closes.append(closes[-1] * math.exp(rng.gauss(0, 0.002)))
        result = fn(_bars(closes))
        assert result >= 0.0
        assert math.isfinite(result)

    @pytest.mark.parametrize(
        "fn", [close_to_close, parkinson, garman_klass, rogers_satchell, yang_zhang]
    )
    def test_empty_input_returns_zero_not_garbage(self, fn) -> None:  # type: ignore[no-untyped-def]
        assert fn([]) == 0.0

    @pytest.mark.parametrize("fn", [close_to_close, yang_zhang])
    def test_return_based_estimators_need_three_bars(self, fn) -> None:  # type: ignore[no-untyped-def]
        """close-to-close and Yang-Zhang consume *differences between* bars.

        Two bars give one return, and one observation has no sample variance, so
        they honestly report 0.0 rather than a number.
        """
        assert fn(_bars([1.10, 1.11])) == 0.0

    @pytest.mark.parametrize("fn", [parkinson, garman_klass, rogers_satchell])
    def test_range_estimators_work_on_a_single_bar(self, fn) -> None:  # type: ignore[no-untyped-def]
        """Deliberately different, and worth stating.

        Range estimators read the high/low WITHIN one bar, so a single bar is a
        valid (if noisy) estimate - that is precisely why they are more efficient.
        The "is this enough data to display?" judgement therefore belongs at the
        API boundary (`_MIN_BARS` in fx_api.routers.market), not in the maths.
        """
        result = fn(_bars([1.10, 1.11]))
        assert result > 0.0
        assert math.isfinite(result)

    def test_range_estimators_are_more_efficient_than_close_to_close(self) -> None:
        """The empirical claim in the docstring, asserted rather than asserted-in-prose.

        Simulate many independent windows of the SAME true process and compare
        how much each estimator's output varies. Less spread = more information
        extracted from the same data.
        """
        rng = random.Random(99)
        true_sigma = 0.01
        steps = 200

        def window() -> list[Bar]:
            price, hi, lo, opening = 1.0, 1.0, 1.0, 1.0
            for _ in range(steps):
                price *= math.exp(rng.gauss(0, true_sigma / math.sqrt(steps)))
                hi, lo = max(hi, price), min(lo, price)
            return [
                Bar("EURUSD", datetime(2026, 8, 19, tzinfo=UTC), opening, hi, lo, price, 0, 0, 0)
            ]

        c2c_est, park_est = [], []
        for _ in range(600):
            bar = window()[0]
            c2c_est.append(math.log(bar.close / bar.open) ** 2)
            park_est.append(math.log(bar.high / bar.low) ** 2 / (4 * math.log(2)))

        cv_c2c = statistics.stdev(c2c_est) / statistics.fmean(c2c_est)
        cv_park = statistics.stdev(park_est) / statistics.fmean(park_est)
        # Efficiency ratio is (cv_c2c / cv_park)^2; the literature says ~5x.
        assert (cv_c2c / cv_park) ** 2 > 3.0
