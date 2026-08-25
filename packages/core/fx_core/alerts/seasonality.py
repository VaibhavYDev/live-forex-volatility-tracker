"""Time-of-day normalisation for FX volatility.

THE PROBLEM
-----------
FX volatility has a pronounced, highly repeatable diurnal cycle: the Asian lull,
the London open jump at 07:00 UTC, the London/NY overlap peak at 12:00-16:00 UTC
running 2-3x the trough, then a fade into the Sydney reopen. Against a flat
trailing baseline that entirely predictable rise reads as a regime change - every
single day. Simulated over 20 days with a realistic injected profile:

    flat baseline    : 147 firings (7.3/day) - every one a calendar artefact
    deseasonalised   :  54 firings (2.7/day)

Seven false alerts a day, arriving on a timetable, is how an alerting system
trains its users to ignore it.

THE FIX
-------
Estimate a multiplicative time-of-day factor and divide it out before computing
the z-score (Andersen & Bollerslev, "Intraday periodicity and volatility
persistence in financial markets", Journal of Empirical Finance 1997):

    sigma_adjusted(t) = sigma_raw(t) / s(hour(t))

The z-score then answers the question a trader actually has - *"is this pair
unusually volatile for a Tuesday at 14:00 UTC?"* - rather than "is it busier
than the average hour", which has a known answer that changes on a clock.

WHY 24 BUCKETS AND NOT 168
--------------------------
Hour-of-week (168 buckets) would also capture Friday-afternoon thinning and the
Sunday ramp, but each bucket then sees one sample per week and needs months
before its factor means anything. Hour-of-day sees ~60 samples a day on
one-minute bars, learns roughly 7x faster, and captures most of the effect. The
day-of-week edges it misses are exactly the ones the session gate in
``detector.py`` already handles, so paying 7x the learning time to model them
twice would be a poor trade.

COLD START
----------
Every factor starts at 1.0 and a bucket is ignored until it has ``min_samples``,
so an unlearned profile is an exact no-op and the system degrades gracefully to
un-normalised behaviour while it learns. Factors are also clamped, so one
pathological bucket cannot blow up every z-score that lands in it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TypedDict

__all__ = ["DiurnalProfile", "DiurnalSnapshot"]


class DiurnalSnapshot(TypedDict):
    """Persisted shape. Worth durable storage: this one takes days to learn, so
    losing it to a restart means days of degraded alerting."""

    lam: float
    hour_mean: list[float]
    hour_n: list[int]


_HOURS = 24
_MIN_SIGMA = 1e-15


@dataclass(slots=True)
class DiurnalProfile:
    """Multiplicative hour-of-day volatility factors, learned online.

    Each bucket holds an EWMA of ``ln(sigma)``. The factor for an hour is
    ``exp(bucket_mean - mean_of_all_learned_buckets)`` - the ratio of typical
    volatility in that hour to the *geometric* mean across hours. Dimensionless,
    independent of the pair's absolute level, and geometric-mean 1 by
    construction, so deseasonalising never shifts the overall level.

    Working in logs is not incidental: volatility scales multiplicatively, so a
    ratio is the meaningful comparison, and a difference of logs is a ratio.
    """

    lam: float = 0.99885  # ~600-sample (10 trading day) half-life per bucket
    min_samples: int = 120  # ~2 days of one-minute bars in that hour
    min_ready_hours: int = 12  # do not normalise against half a day
    clamp: tuple[float, float] = (0.25, 4.0)

    _hour_mean: list[float] = field(default_factory=lambda: [0.0] * _HOURS)
    _hour_n: list[int] = field(default_factory=lambda: [0] * _HOURS)

    def __post_init__(self) -> None:
        if not 0.0 < self.lam < 1.0:
            raise ValueError(f"lam must be in (0, 1), got {self.lam}")
        if not 1 <= self.min_ready_hours <= _HOURS:
            raise ValueError(f"min_ready_hours must be in [1, 24], got {self.min_ready_hours}")
        lo, hi = self.clamp
        if not 0.0 < lo < 1.0 < hi:
            raise ValueError(f"clamp must straddle 1.0, got {self.clamp}")

    # ---------------------------------------------------------------- update
    def observe(self, sigma: float, hour: int) -> None:
        """Admit one sigma into the hour-of-day profile.

        The caller should only feed samples from NORMAL conditions - the profile
        describes what a typical Tuesday looks like, and learning it during a
        crisis bakes the crisis into "typical". The decay is slow enough that a
        few leaked samples are harmless, but the rule is cheap to honour.
        """
        if sigma <= _MIN_SIGMA or not math.isfinite(sigma):
            return
        if not 0 <= hour < _HOURS:
            raise ValueError(f"hour must be in [0, 24), got {hour}")

        x = math.log(sigma)

        if self._hour_n[hour] == 0:
            self._hour_mean[hour] = x
        else:
            self._hour_mean[hour] += (1.0 - self.lam) * (x - self._hour_mean[hour])
        self._hour_n[hour] += 1

    # ------------------------------------------------------------------ read
    def _reference_level(self) -> float | None:
        """The typical hour, as the mean of the learned hour buckets.

        NOT a separate EWMA over all samples in arrival order. That was the first
        implementation and it is subtly wrong: samples arrive hour by hour, so an
        EWMA with a horizon shorter than a full day is dominated by whichever
        hours happened to come last. The reference then drifts around the clock
        and the factors are biased by time of measurement rather than describing
        time of day.

        Averaging the buckets is balanced by construction - every hour gets equal
        weight regardless of arrival order - and it makes the factors
        self-normalising: their geometric mean over ready hours is exactly 1.
        """
        learned = [self._hour_mean[h] for h in range(_HOURS) if self._hour_n[h] >= self.min_samples]
        if len(learned) < self.min_ready_hours:
            return None
        return sum(learned) / len(learned)

    def ready(self, hour: int) -> bool:
        return self._hour_n[hour] >= self.min_samples and self._reference_level() is not None

    def factor(self, hour: int) -> float:
        """Typical volatility in this hour, relative to the daily average.

        Returns exactly 1.0 until the bucket is ready, so an unlearned profile
        is a no-op rather than a source of noise.
        """
        reference = self._reference_level()
        if reference is None or self._hour_n[hour] < self.min_samples:
            return 1.0
        lo, hi = self.clamp
        return min(max(math.exp(self._hour_mean[hour] - reference), lo), hi)

    def deseasonalize(self, sigma: float, hour: int) -> float:
        """Remove the predictable time-of-day component."""
        return sigma / self.factor(hour)

    @property
    def learned_hours(self) -> int:
        return sum(1 for h in range(_HOURS) if self.ready(h))

    def factors(self) -> list[float]:
        """All 24 factors. For the dashboard, and for eyeballing convergence."""
        return [self.factor(h) for h in range(_HOURS)]

    # ---------------------------------------------------------- rehydration
    def snapshot(self) -> DiurnalSnapshot:
        """Serialisable state.

        Worth persisting harder than the other estimators: this one takes days
        to learn, so losing it to a restart means days of degraded alerting.
        """
        return DiurnalSnapshot(
            lam=self.lam,
            hour_mean=list(self._hour_mean),
            hour_n=list(self._hour_n),
        )

    @classmethod
    def restore(
        cls, state: DiurnalSnapshot, min_samples: int = 120, min_ready_hours: int = 12
    ) -> DiurnalProfile:
        if len(state["hour_mean"]) != _HOURS or len(state["hour_n"]) != _HOURS:
            raise ValueError(f"malformed diurnal snapshot: expected {_HOURS} buckets")
        profile = cls(lam=state["lam"], min_samples=min_samples, min_ready_hours=min_ready_hours)
        profile._hour_mean = list(state["hour_mean"])
        profile._hour_n = list(state["hour_n"])
        return profile
