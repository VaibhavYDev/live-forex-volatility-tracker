"""Regime detection maths.

Reference values are computed independently — from numpy, from the closed-form
definition of an exponentially weighted moment, or from the statistical property
the estimator is supposed to have. Never from the implementation. A test that
reimplements the code it tests proves only that you typed it twice.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import numpy as np
import pytest
from fx_core.alerts import (
    DetectorConfig,
    DiurnalProfile,
    Regime,
    RegimeDetector,
    SchmittTrigger,
    TransitionCause,
    TriggerConfig,
    VolatilityBaseline,
    lambda_for_halflife,
    robust_zscore,
)

T0 = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)  # a Wednesday, market open


def lognormal(n: int, median: float = 1e-4, log_sd: float = 0.30, seed: int = 0) -> list[float]:
    """Realised volatility is approximately lognormal (Andersen et al. 2001)."""
    rng = np.random.default_rng(seed)
    return list(np.exp(rng.normal(math.log(median), log_sd, n)))


# =============================================================== baseline ===
class TestVolatilityBaseline:
    def test_matches_closed_form_exponentially_weighted_moments(self) -> None:
        """Compare against the DEFINITION, not against the recurrence.

        The implementation uses Finch's O(1) incremental form; this computes the
        same quantities from their definition with explicit geometric weights in
        numpy. Agreement means the recurrence is algebraically right, which a
        test written in the same style as the code could never establish.
        """
        lam = 0.99
        xs = lognormal(4000, seed=1)
        baseline = VolatilityBaseline(lam=lam, min_samples=10)
        for x in xs:
            baseline.observe(x)

        logs = np.log(np.array(xs))
        n = len(logs)
        weights = lam ** np.arange(n - 1, -1, -1)  # oldest gets the smallest weight
        weights /= weights.sum()
        ref_mean = float(np.sum(weights * logs))
        ref_var = float(np.sum(weights * (logs - ref_mean) ** 2))

        assert baseline.mean == pytest.approx(ref_mean, rel=1e-6)
        assert baseline.stdev == pytest.approx(math.sqrt(ref_var), rel=0.02)

    def test_recovers_the_true_generating_parameters(self) -> None:
        xs = lognormal(20_000, median=2.5e-4, log_sd=0.42, seed=2)
        baseline = VolatilityBaseline(lam=lambda_for_halflife(2000), min_samples=30)
        for x in xs:
            baseline.observe(x)

        assert baseline.level == pytest.approx(2.5e-4, rel=0.15)
        assert baseline.stdev == pytest.approx(0.42, rel=0.15)

    def test_log_space_z_has_a_near_gaussian_tail_where_linear_z_does_not(self) -> None:
        """THE headline result. Linear z on lognormal data fires ~13x too often.

        Volatility has a hard floor at zero and a long right tail, so a linear
        z-score's "3 sigma" is nowhere near a 1-in-740 event. Measured here on
        pure noise containing NO regime change whatsoever — every firing is a
        false positive by construction.
        """
        xs = lognormal(60_000, seed=3)
        window = 120
        gauss_rate = 0.00135  # P(Z >= 3), one-sided

        log_hits = linear_hits = trials = 0
        for i in range(window, len(xs), 7):  # stride to keep the test quick
            win = np.array(xs[i - window : i])
            trials += 1

            lg = np.log(win)
            if lg.std(ddof=1) > 0 and (math.log(xs[i]) - lg.mean()) / lg.std(ddof=1) >= 3.0:
                log_hits += 1
            if win.std(ddof=1) > 0 and (xs[i] - win.mean()) / win.std(ddof=1) >= 3.0:
                linear_hits += 1

        log_rate, linear_rate = log_hits / trials, linear_hits / trials
        print(
            f"\n  linear z    P(z>=3) = {linear_rate * 100:6.3f}%  "
            f"= {linear_rate / gauss_rate:5.1f}x Gaussian"
        )
        print(
            f"  log-space z P(z>=3) = {log_rate * 100:6.3f}%  "
            f"= {log_rate / gauss_rate:5.1f}x Gaussian"
        )

        assert log_rate < gauss_rate * 4, "log-space z is not close to its nominal rate"
        assert linear_rate > log_rate * 3, "linear z should be dramatically worse"

    def test_z_is_scale_invariant(self) -> None:
        """One threshold must work for a pair at 5% vol and one at 15%.

        A doubling of volatility is the same event regardless of the resting
        level; only a log-space statistic has that property.
        """
        xs = lognormal(3000, median=1e-4, seed=4)
        quiet = VolatilityBaseline(min_samples=30)
        busy = VolatilityBaseline(min_samples=30)
        for x in xs:
            quiet.observe(x)
            busy.observe(x * 37.0)  # same shape, wildly different level

        z_quiet = quiet.zscore(1e-4 * 3)
        z_busy = busy.zscore(1e-4 * 37.0 * 3)
        assert z_quiet is not None and z_busy is not None
        assert z_quiet == pytest.approx(z_busy, rel=1e-9)

    def test_freezing_stops_the_yardstick_from_stretching(self) -> None:
        """Volatility clustering would otherwise clear the alert on its own.

        During a persistent stress event, elevated samples enter the baseline,
        the mean AND the spread both rise, and z decays toward zero while the
        market is still stressed. The freeze is what makes the baseline mean
        "what normal looked like BEFORE this event".
        """
        calm = lognormal(600, median=1e-4, seed=5)
        stress = lognormal(400, median=5e-4, seed=6)

        rolling = VolatilityBaseline(lam=lambda_for_halflife(120), min_samples=30)
        frozen = VolatilityBaseline(lam=lambda_for_halflife(120), min_samples=30)
        for x in calm:
            rolling.observe(x)
            frozen.observe(x)
        frozen.freeze()

        z_rolling = z_frozen = None
        for x in stress:
            rolling.observe(x)
            frozen.observe(x)  # ignored — frozen
            z_rolling, z_frozen = rolling.zscore(x), frozen.zscore(x)

        assert z_rolling is not None and z_frozen is not None
        print(f"\n  after 400 stressed bars: rolling z={z_rolling:.2f}  frozen z={z_frozen:.2f}")
        assert z_rolling < 1.5, "rolling baseline should have absorbed the stress (the bug)"
        assert z_frozen > 3.0, "frozen baseline should still report stress (the fix)"

    def test_robust_oracle_agrees_on_stationary_data(self) -> None:
        """Median/MAD is an independent estimator of the same quantity.

        It shares no code path with the EWMA recurrence, so agreement is real
        evidence rather than a tautology.
        """
        xs = lognormal(2000, seed=7)
        baseline = VolatilityBaseline(lam=lambda_for_halflife(1000), min_samples=30)
        for x in xs:
            baseline.observe(x)

        for probe in (0.6e-4, 1.0e-4, 2.0e-4, 4.0e-4):
            ewma_z = baseline.zscore(probe)
            robust_z = robust_zscore(xs, probe)
            assert ewma_z is not None and robust_z is not None
            assert ewma_z == pytest.approx(robust_z, abs=0.35), f"disagree at sigma={probe}"

    def test_not_ready_below_min_samples(self) -> None:
        baseline = VolatilityBaseline(min_samples=30)
        for x in lognormal(29, seed=8):
            baseline.observe(x)
        assert not baseline.ready
        assert baseline.zscore(1e-4) is None, "must distinguish 'not unusual' from 'cannot say'"

    @pytest.mark.parametrize("bad", [0.0, -1e-4, float("nan"), float("inf")])
    def test_degenerate_sigma_cannot_poison_the_estimator(self, bad: float) -> None:
        """A frozen feed produces sigma == 0 exactly; ln(0) would be permanent."""
        baseline = VolatilityBaseline(min_samples=10)
        for x in lognormal(200, seed=9):
            baseline.observe(x)
        before = (baseline.mean, baseline.var, baseline.n)

        baseline.observe(bad)
        assert (baseline.mean, baseline.var, baseline.n) == before
        assert baseline.zscore(bad) is None
        assert math.isfinite(baseline.mean)

    def test_rejects_overlapping_fast_and_slow_horizons(self) -> None:
        """If the fast series is a big share of its own baseline, z collapses."""
        baseline = VolatilityBaseline(lam=lambda_for_halflife(120))
        baseline.assert_separated_from(fast_halflife=15)  # 8x — fine
        with pytest.raises(ValueError, match="measuring itself"):
            baseline.assert_separated_from(fast_halflife=60)  # 2x — not fine

    def test_snapshot_restore_round_trip(self) -> None:
        original = VolatilityBaseline(min_samples=30)
        for x in lognormal(500, seed=10):
            original.observe(x)
        original.freeze()

        restored = VolatilityBaseline.restore(original.snapshot(), min_samples=30)
        assert restored.zscore(3e-4) == pytest.approx(original.zscore(3e-4))
        assert restored.frozen and restored.n == original.n


# ============================================================ seasonality ===
class TestDiurnalProfile:
    def test_cold_start_is_an_exact_no_op(self) -> None:
        """An unlearned profile must not perturb anything."""
        profile = DiurnalProfile(min_samples=100)
        assert profile.factors() == [1.0] * 24
        assert profile.deseasonalize(1.234e-4, 13) == 1.234e-4

    def test_learns_the_injected_intraday_shape(self) -> None:
        rng = random.Random(11)
        truth = {h: (2.5 if 12 <= h < 17 else 1.8 if 7 <= h < 12 else 0.7) for h in range(24)}
        profile = DiurnalProfile(lam=lambda_for_halflife(200), min_samples=50)

        for _ in range(40):  # 40 simulated days
            for hour in range(24):
                for _ in range(30):
                    profile.observe(1e-4 * truth[hour] * math.exp(rng.gauss(0, 0.25)), hour)

        # Factors are ratios to the GEOMETRIC mean, because the profile works in
        # log space. Using the arithmetic mean here would be the wrong oracle.
        learned = profile.factors()
        geo_mean = math.exp(sum(math.log(v) for v in truth.values()) / 24)
        for hour in range(24):
            assert learned[hour] == pytest.approx(truth[hour] / geo_mean, rel=0.12)
        assert profile.learned_hours == 24
        # Self-normalising: deseasonalising must not shift the overall level.
        assert math.exp(sum(math.log(f) for f in learned) / 24) == pytest.approx(1.0, abs=0.02)

    def test_deseasonalizing_removes_the_hour_of_day_dependence(self) -> None:
        """The property that matters, checked with numpy rather than by eye."""
        rng = random.Random(12)
        truth = {h: (2.5 if 12 <= h < 17 else 0.8) for h in range(24)}
        profile = DiurnalProfile(lam=lambda_for_halflife(200), min_samples=50)
        for _ in range(40):
            for hour in range(24):
                for _ in range(30):
                    profile.observe(1e-4 * truth[hour] * math.exp(rng.gauss(0, 0.2)), hour)

        raw = {h: [] for h in range(24)}
        adj = {h: [] for h in range(24)}
        for _ in range(5):
            for hour in range(24):
                for _ in range(30):
                    s = 1e-4 * truth[hour] * math.exp(rng.gauss(0, 0.2))
                    raw[hour].append(math.log(s))
                    adj[hour].append(math.log(profile.deseasonalize(s, hour)))

        spread_raw = float(np.std([np.mean(raw[h]) for h in range(24)]))
        spread_adj = float(np.std([np.mean(adj[h]) for h in range(24)]))
        print(f"\n  spread of hourly means: raw={spread_raw:.3f}  deseasonalized={spread_adj:.3f}")
        assert spread_adj < spread_raw / 5, "time-of-day structure survived normalisation"

    def test_factor_is_clamped(self) -> None:
        """One pathological bucket must not blow up every z-score in that hour."""
        profile = DiurnalProfile(lam=0.5, min_samples=2, min_ready_hours=2, clamp=(0.5, 2.0))
        for _ in range(50):
            profile.observe(1e-4, 3)
        for _ in range(50):
            profile.observe(1e2, 4)  # absurd
        assert 0.5 <= profile.factor(4) <= 2.0

    def test_rejects_an_out_of_range_hour(self) -> None:
        with pytest.raises(ValueError, match="hour must be"):
            DiurnalProfile().observe(1e-4, 24)

    def test_snapshot_restore_round_trip(self) -> None:
        original = DiurnalProfile(min_samples=10, min_ready_hours=12)
        for _ in range(30):
            for hour in range(24):
                original.observe(1e-4 * (2.0 if hour == 14 else 1.0), hour)
        restored = DiurnalProfile.restore(original.snapshot(), min_samples=10)
        assert restored.factors() == pytest.approx(original.factors())


# ============================================================== trigger ====
def feed(
    trigger: SchmittTrigger, zs: list[float], start: datetime = T0, step_s: int = 60
) -> list[tuple[int, str, str]]:
    out = []
    for i, z in enumerate(zs):
        ev = trigger.update(z, 1e-4, start + timedelta(seconds=i * step_s))
        if ev:
            out.append((i, str(ev.old_regime), str(ev.new_regime)))
    return out


class TestSchmittTrigger:
    def test_hysteresis_band_holds_the_current_regime(self) -> None:
        """Between exit_z and enter_z, neither threshold applies. That IS the band."""
        cfg = TriggerConfig(
            enter_z=3.0, exit_z=1.5, min_confirm_samples=1, confirm_for=timedelta(0)
        )
        trigger = SchmittTrigger("EURUSD", cfg)

        assert feed(trigger, [4.0]) == [(0, "normal", "stressed")]
        assert feed(trigger, [2.9, 2.0, 1.6, 2.5]) == []  # inside the band: still stressed
        assert trigger.regime is Regime.STRESSED
        assert feed(trigger, [1.4]) == [(0, "stressed", "normal")]

    def test_hysteresis_beats_a_single_threshold_on_an_oscillating_signal(self) -> None:
        cfg = TriggerConfig(
            enter_z=3.0, exit_z=1.5, min_confirm_samples=1, confirm_for=timedelta(0)
        )
        trigger = SchmittTrigger("EURUSD", cfg)
        oscillating = [2.9, 3.1] * 30

        naive = sum(1 for a, b in pairwise(oscillating) if (a >= 3) != (b >= 3))
        events = feed(trigger, oscillating)

        print(f"\n  single threshold: {naive} crossings   schmitt: {len(events)} transitions")
        assert naive > 50
        assert len(events) == 1

    def test_confirmation_is_symmetric(self) -> None:
        """The defect this version fixes: one low print used to clear instantly.

        Requiring 3 samples to fire but 1 to clear is not noise protection, it is
        noise protection pointed in one direction.
        """
        cfg = TriggerConfig(min_confirm_samples=3, confirm_for=timedelta(minutes=2))
        trigger = SchmittTrigger("EURUSD", cfg)

        assert len(feed(trigger, [5.0] * 4)) == 1
        assert trigger.regime is Regime.STRESSED

        # A single spuriously low sample mid-crisis must NOT disarm the alert.
        assert feed(trigger, [0.5, 5.0, 5.0, 5.0]) == []
        assert trigger.regime is Regime.STRESSED

        assert len(feed(trigger, [0.5] * 4)) == 1
        assert trigger.regime is Regime.NORMAL

    def test_confirmation_requires_wall_clock_time_not_just_samples(self) -> None:
        """Bars seal on tick arrival, so N samples can span seconds or an hour."""
        cfg = TriggerConfig(
            min_confirm_samples=3,
            confirm_for=timedelta(minutes=5),
            sample_timeout=timedelta(minutes=20),
        )
        trigger = SchmittTrigger("EURUSD", cfg)

        assert feed(trigger, [5.0] * 10, step_s=1) == [], "10 samples in 10s must not commit"
        assert trigger.regime is Regime.NORMAL

        assert len(feed(trigger, [5.0] * 6, start=T0 + timedelta(minutes=1), step_s=60)) == 1

    def test_a_gap_longer_than_sample_timeout_abandons_the_candidate(self) -> None:
        """Samples straddling an outage are not consecutive observations."""
        cfg = TriggerConfig(
            min_confirm_samples=3,
            confirm_for=timedelta(minutes=2),
            sample_timeout=timedelta(minutes=5),
        )
        trigger = SchmittTrigger("EURUSD", cfg)

        trigger.update(5.0, 1e-4, T0)
        trigger.update(5.0, 1e-4, T0 + timedelta(minutes=1))
        assert trigger.candidate is Regime.STRESSED

        # ...a 20 minute hole...
        trigger.update(5.0, 1e-4, T0 + timedelta(minutes=21))
        assert trigger.candidate is Regime.STRESSED
        # Candidate restarted, so this third-since-restart sample is the one that
        # would have committed under the old sample-counting rule.
        assert trigger.update(5.0, 1e-4, T0 + timedelta(minutes=22)) is None
        assert trigger.regime is Regime.NORMAL

    def test_cooldown_limits_escalations_but_never_delays_a_clear(self) -> None:
        """A stale STRESSED banner is worse than an extra line in the alert log."""
        cfg = TriggerConfig(
            min_confirm_samples=1, confirm_for=timedelta(0), cooldown=timedelta(minutes=30)
        )
        trigger = SchmittTrigger("EURUSD", cfg)

        assert len(feed(trigger, [5.0])) == 1
        assert len(feed(trigger, [0.0], start=T0 + timedelta(minutes=1))) == 1, "clear not delayed"
        assert feed(trigger, [5.0], start=T0 + timedelta(minutes=2)) == [], (
            "escalation rate-limited"
        )
        assert len(feed(trigger, [5.0], start=T0 + timedelta(minutes=45))) == 1

    def test_transitions_alternate_and_seq_is_monotonic(self) -> None:
        """The invariant a persister can check: a hole means an event was dropped."""
        rng = random.Random(13)
        cfg = TriggerConfig(min_confirm_samples=2, confirm_for=timedelta(minutes=1))
        trigger = SchmittTrigger("EURUSD", cfg)

        transitions = []
        for i in range(4000):
            ev = trigger.update(rng.gauss(1.8, 2.2), 1e-4, T0 + timedelta(minutes=i))
            if ev:
                transitions.append(ev)

        assert len(transitions) > 10, "test signal produced too few transitions to be meaningful"
        assert [t.seq for t in transitions] == list(range(1, len(transitions) + 1))
        for prev, nxt in pairwise(transitions):
            assert prev.new_regime == nxt.old_regime, "alternation invariant violated"
            assert nxt.new_regime != prev.new_regime

    def test_gated_updates_hold_the_regime_rather_than_asserting_calm(self) -> None:
        """'We stopped observing' is not evidence that the market calmed down."""
        cfg = TriggerConfig(min_confirm_samples=1, confirm_for=timedelta(0))
        trigger = SchmittTrigger("EURUSD", cfg)
        feed(trigger, [5.0])
        assert trigger.regime is Regime.STRESSED

        for i in range(5):
            assert (
                trigger.update(
                    None, 0.0, T0 + timedelta(minutes=i + 1), armed=False, gate_reason="feed stale"
                )
                is None
            )
        assert trigger.regime is Regime.STRESSED
        assert not trigger.armed
        assert trigger.gate_reason == "feed stale"

    def test_prolonged_blindness_stops_asserting_a_regime(self) -> None:
        cfg = TriggerConfig(
            min_confirm_samples=1,
            confirm_for=timedelta(0),
            observation_timeout=timedelta(minutes=30),
        )
        trigger = SchmittTrigger("EURUSD", cfg)
        feed(trigger, [5.0])

        for i in range(1, 29):
            assert trigger.update(None, 0.0, T0 + timedelta(minutes=i), armed=False) is None

        ev = trigger.update(None, 0.0, T0 + timedelta(minutes=40), armed=False)
        assert ev is not None
        assert ev.new_regime is Regime.NORMAL
        assert ev.cause is TransitionCause.OBSERVATION_LOST

    def test_thaw_is_recorded_as_a_rebaseline_not_as_calm(self) -> None:
        """Those are different claims and only one of them is true."""
        cfg = TriggerConfig(
            min_confirm_samples=1, confirm_for=timedelta(0), thaw_after=timedelta(hours=6)
        )
        trigger = SchmittTrigger("EURUSD", cfg)
        feed(trigger, [5.0])

        assert not trigger.baseline_should_thaw(T0 + timedelta(hours=5))
        assert trigger.baseline_should_thaw(T0 + timedelta(hours=7))

        trigger.mark_thawed()
        ev = trigger.update(0.0, 1e-4, T0 + timedelta(hours=7))
        assert ev is not None
        assert ev.cause is TransitionCause.BASELINE_THAW
        assert ev.new_regime is Regime.NORMAL

    def test_transition_carries_the_schema_fields(self) -> None:
        trigger = SchmittTrigger(
            "GBPJPY", TriggerConfig(min_confirm_samples=1, confirm_for=timedelta(0))
        )
        ev = trigger.update(4.2, 3.3e-4, T0)
        assert ev is not None
        assert (ev.symbol, ev.ts, ev.old_regime, ev.new_regime) == (
            "GBPJPY",
            T0,
            Regime.NORMAL,
            Regime.STRESSED,
        )
        assert ev.trigger_value == pytest.approx(4.2)
        assert ev.threshold_value == pytest.approx(3.0)
        assert ev.is_escalation

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"enter_z": 3.0, "exit_z": 3.0}, "must be below"),
            ({"enter_z": 1.0, "exit_z": 2.0}, "must be below"),
            ({"min_confirm_samples": 0}, "at least 1"),
            ({"sample_timeout": timedelta(0)}, "must be positive"),
            (
                {"confirm_for": timedelta(minutes=10), "sample_timeout": timedelta(minutes=5)},
                "no candidate can ever complete",
            ),
        ],
    )
    def test_config_rejects_self_defeating_settings(self, kwargs: dict, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            TriggerConfig(**kwargs)

    def test_snapshot_restore_survives_a_failover(self) -> None:
        """A cold standby would re-fire the escalation for an already-stressed market."""
        cfg = TriggerConfig(min_confirm_samples=1, confirm_for=timedelta(0))
        original = SchmittTrigger("EURUSD", cfg)
        feed(original, [5.0])

        restored = SchmittTrigger.restore("EURUSD", original.snapshot(), cfg)
        assert restored.regime is Regime.STRESSED
        assert restored.seq == original.seq
        # Still stressed => no duplicate escalation emitted.
        assert restored.update(5.0, 1e-4, T0 + timedelta(minutes=5)) is None


# ======================================================= noisy sine wave ====
class TestNoisySineWave:
    """The requested proof, at the maths level.

    A slow sine wave is the macro regime; heavy Gaussian noise is layered on top
    with an amplitude comparable to the signal itself. A correct detector fires
    exactly twice per period — once entering the crest, once leaving — and
    ignores every noise excursion in between.

    (The full pipeline version, driving the ingestor through Redis, belongs in
    tests/chaos/ once the wiring lands.)
    """

    @staticmethod
    def _signal(periods: int, per_period: int, noise_sd: float, seed: int) -> list[float]:
        rng = random.Random(seed)
        return [
            4.0 * math.sin(2 * math.pi * i / per_period) + rng.gauss(0.0, noise_sd)
            for i in range(periods * per_period)
        ]

    def test_fires_only_at_the_macro_boundaries(self) -> None:
        periods, per_period, noise_sd = 6, 240, 1.6
        zs = self._signal(periods, per_period, noise_sd, seed=14)

        cfg = TriggerConfig(
            enter_z=3.0,
            exit_z=1.5,
            min_confirm_samples=5,
            confirm_for=timedelta(minutes=5),
            sample_timeout=timedelta(minutes=20),
            cooldown=timedelta(minutes=10),
        )
        trigger = SchmittTrigger("EURUSD", cfg)
        events = [
            ev
            for i, z in enumerate(zs)
            if (ev := trigger.update(z, 1e-4, T0 + timedelta(minutes=i))) is not None
        ]

        naive = sum(1 for a, b in pairwise(zs) if (a >= 3.0) != (b >= 3.0))
        escalations = [e for e in events if e.is_escalation]

        print(f"\n  noise sd={noise_sd} on amplitude 4.0 over {periods} periods")
        print(f"  single threshold : {naive:4d} crossings")
        print(f"  schmitt trigger  : {len(events):4d} transitions ({len(escalations)} escalations)")

        assert naive > 40, "noise level too low for this test to prove anything"
        assert len(escalations) == periods, "should escalate exactly once per crest"
        assert len(events) in (2 * periods - 1, 2 * periods)

        # Every escalation must land near a true crest, not in a trough.
        for ev in escalations:
            phase = (ev.ts - T0).total_seconds() / 60 % per_period
            assert 0.1 < phase / per_period < 0.6, f"escalated at phase {phase / per_period:.2f}"

    def test_survives_noise_larger_than_the_signal(self) -> None:
        """Degrade, do not shatter: more noise may cost transitions, never storms."""
        zs = self._signal(6, 240, noise_sd=5.0, seed=15)
        cfg = TriggerConfig(
            min_confirm_samples=8,
            confirm_for=timedelta(minutes=8),
            sample_timeout=timedelta(minutes=30),
            cooldown=timedelta(minutes=20),
        )
        trigger = SchmittTrigger("EURUSD", cfg)
        events = [
            ev
            for i, z in enumerate(zs)
            if (ev := trigger.update(z, 1e-4, T0 + timedelta(minutes=i))) is not None
        ]
        naive = sum(1 for a, b in pairwise(zs) if (a >= 3.0) != (b >= 3.0))

        print(f"\n  noise sd=5.0 (> signal): single threshold {naive}, schmitt {len(events)}")
        assert naive > 200
        assert len(events) <= 16, "hysteresis must not degenerate into a storm"


# ============================================================== detector ====
class TestRegimeDetector:
    @staticmethod
    def _warm(detector: RegimeDetector, bars: int = 400, seed: int = 16) -> datetime:
        ts = T0
        for x in lognormal(bars, seed=seed):
            detector.update(x, ts)
            ts += timedelta(minutes=1)
        return ts

    def test_no_alerts_while_the_market_is_closed(self) -> None:
        detector = RegimeDetector("EURUSD", market_is_open=lambda _ts: False)
        for i in range(200):
            assert detector.update(9e-3, T0 + timedelta(minutes=i)) is None
        assert detector.regime is Regime.NORMAL
        assert not detector.armed

    def test_the_sunday_reopen_gap_does_not_fire(self) -> None:
        """z on a real weekend gap is ~11. Ungated, this fires every Sunday."""
        open_at = {"value": True}
        detector = RegimeDetector(
            "EURUSD",
            config=DetectorConfig(session_warmup=timedelta(minutes=30)),
            market_is_open=lambda _ts: open_at["value"],
        )
        ts = self._warm(detector)

        open_at["value"] = False  # Friday close
        for i in range(60):
            detector.update(1e-4, ts + timedelta(minutes=i))
        ts += timedelta(minutes=60)

        open_at["value"] = True  # Sunday reopen — the gap bar lands first
        assert detector.update(3.9e-3, ts, is_gap_bar=True) is None
        for i in range(1, 25):
            assert detector.update(1.1e-4, ts + timedelta(minutes=i)) is None, (
                "fired during warm-up"
            )
        assert detector.regime is Regime.NORMAL

    def test_backfilled_bars_are_never_evaluated(self) -> None:
        detector = RegimeDetector("EURUSD")
        ts = self._warm(detector)
        for i in range(30):
            assert detector.update(9e-3, ts + timedelta(minutes=i), is_backfill=True) is None
        assert detector.regime is Regime.NORMAL

    def test_a_stale_feed_never_reports_a_volatility_collapse(self) -> None:
        detector = RegimeDetector("EURUSD")
        ts = self._warm(detector)
        for i in range(20):
            assert detector.update(0.0, ts + timedelta(minutes=i), feed_is_stale=True) is None
        assert detector.regime is Regime.NORMAL

    def test_detects_a_genuine_regime_shift_and_stays_stressed(self) -> None:
        """The end-to-end property: fire once, and do not disarm yourself."""
        detector = RegimeDetector(
            "EURUSD",
            # thaw_after is set beyond the run length ON PURPOSE, to isolate the
            # property under test. 500 one-minute bars is 8.3 hours, so a default
            # 6h thaw would end the event and mask whether clustering had already
            # disarmed the detector - which is the actual thing being asserted.
            trigger_config=TriggerConfig(
                min_confirm_samples=3,
                confirm_for=timedelta(minutes=3),
                thaw_after=timedelta(hours=24),
            ),
        )
        ts = self._warm(detector, bars=600, seed=17)

        events = []
        for x in lognormal(500, median=6e-4, log_sd=0.30, seed=18):
            ev = detector.update(x, ts)
            if ev:
                events.append(ev)
            ts += timedelta(minutes=1)

        assert len(events) == 1, (
            f"expected one escalation, got {[str(e.new_regime) for e in events]}"
        )
        assert events[0].new_regime is Regime.STRESSED
        assert detector.regime is Regime.STRESSED, "detector disarmed itself during the event"

    def test_persistent_stress_is_eventually_accepted_as_the_new_normal(self) -> None:
        """A frozen baseline held forever is its own bug."""
        detector = RegimeDetector(
            "EURUSD",
            trigger_config=TriggerConfig(
                min_confirm_samples=2,
                confirm_for=timedelta(minutes=2),
                thaw_after=timedelta(hours=2),
            ),
        )
        ts = self._warm(detector, bars=600, seed=19)

        events = []
        for x in lognormal(900, median=6e-4, log_sd=0.30, seed=20):
            ev = detector.update(x, ts)
            if ev:
                events.append(ev)
            ts += timedelta(minutes=1)

        assert [str(e.new_regime) for e in events] == ["stressed", "normal"]
        assert events[1].cause is TransitionCause.BASELINE_THAW
        assert detector.regime is Regime.NORMAL

    def test_quiet_market_produces_no_alerts_at_all(self) -> None:
        """The rate that decides whether anyone keeps the notifications on."""
        detector = RegimeDetector("EURUSD")
        ts, fired = T0, 0
        for x in lognormal(20_000, seed=21):
            if detector.update(x, ts) is not None:
                fired += 1
            ts += timedelta(minutes=1)

        days = 20_000 / 1440
        print(f"\n  {fired} transitions over {days:.1f} simulated days of pure noise")
        assert fired <= 2, f"{fired} false transitions on stationary data"

    def test_snapshot_restore_preserves_the_whole_detection_path(self) -> None:
        detector = RegimeDetector("EURUSD")
        ts = self._warm(detector, bars=500, seed=22)
        state = detector.snapshot()

        revived = RegimeDetector("EURUSD")
        revived.restore(state)
        assert revived.regime is detector.regime
        assert revived.baseline.zscore(3e-4) == pytest.approx(detector.baseline.zscore(3e-4))
        assert revived.update(1e-4, ts + timedelta(minutes=1)) is None
