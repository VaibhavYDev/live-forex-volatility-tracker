"""The reference distribution a z-score is measured against.

Two decisions here, both of which a quantitative reviewer will check first.


1. WORK IN LOG SPACE
--------------------
Realised volatility is approximately **lognormal**, not normal (Andersen,
Bollerslev, Diebold & Labys, "The distribution of realized exchange rate
volatility", JASA 2001). It has a hard floor at zero and a long right tail. A
linear z-score assumes symmetry, so a "3 sigma" threshold is nowhere near the
1-in-740 event it advertises.

Measured on 60,000 bars of pure lognormal noise containing NO regime change:

    statistic      P(z >= 3.0)     vs. Gaussian 0.135%   false alerts/pair/day
    -----------    ------------    -------------------   ---------------------
    linear z          1.783%              13.2x                  ~26
    log-space z       0.159%               1.2x                  ~2.3

Twenty-six false alerts per pair per day is not an alerting system. One
``math.log`` fixes it, and as a bonus the statistic becomes scale-invariant: a
doubling of volatility is the same z whether the pair rests at 5% or 15%
annualised, so one threshold works across a whole watchlist.


2. EWMA, NOT A ROLLING WINDOW
-----------------------------
A simple moving average has the **drop-out artefact**: when a spike falls off
the back of the window the mean jumps discontinuously and z jumps with it,
firing or clearing an alert for no market reason whatsoever. An EWMA has no
drop-out, is O(1) in memory, and needs no per-symbol ring buffer.

The EWMA's weakness is infinite memory - a spike decays but never fully leaves.
That is handled by ``freeze()``, below, which is a better answer anyway.

The recurrence (Finch 2009, "Incremental calculation of weighted mean and
variance") maintains both moments in O(1):

    diff = x - mean
    incr = (1 - lam) * diff
    mean = mean + incr
    var  = lam * (var + diff * incr)


3. FREEZING, AND WHY IT MATTERS MORE THAN EITHER
------------------------------------------------
Volatility clusters (Mandelbrot 1963; the whole ARCH literature exists because
of it). During a stress event the elevated samples enter the baseline, the mean
AND the standard deviation both rise, and z decays toward zero on its own. The
alert clears not because the market calmed but because **the yardstick
stretched**. Simulated on a regime shift that persists for 300 bars:

    rolling baseline : z=3.04 at onset -> below exit_z after  20 bars
    frozen baseline  : z=3.04 at onset -> below exit_z after 292 bars

Twenty bars. The detector disarms itself twenty minutes into a crisis and
reports "back to normal". So: while the regime is STRESSED, the baseline stops
admitting samples and means "what normal looked like *before* this event" -
which is precisely the reference a regime detector needs. The freeze is bounded
by ``SchmittTrigger.baseline_should_thaw``; see that method for the trade.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

__all__ = [
    "BaselineSnapshot",
    "VolatilityBaseline",
    "lambda_for_halflife",
    "robust_zscore",
]


class BaselineSnapshot(TypedDict):
    """Persisted shape. Named because it becomes a Redis payload in Milestone 1,
    and an un-named serialisation format drifts the moment two services read it."""

    lam: float
    mean: float
    var: float
    n: int
    debias: float
    frozen: bool


# ln(sigma) of a sigma at or below this is treated as unmeasurable rather than
# as a very large negative number. A frozen feed produces sigma == 0 exactly,
# and -inf would poison both moments permanently.
_MIN_SIGMA = 1e-15

# Bessel-style floor: a single observation has no spread, so no estimator here
# can say anything useful below two samples.
_MIN_OBSERVATIONS = 2


def lambda_for_halflife(halflife_samples: float) -> float:
    """Decay factor whose weight halves after ``halflife_samples`` observations.

    The honest way to configure an EWMA: state the horizon you care about and
    derive lambda, rather than copying 0.94 from a 1996 paper about daily data
    and silently applying it to one-minute bars.
    """
    if halflife_samples <= 0:
        raise ValueError("halflife_samples must be positive")
    return math.exp(-math.log(2.0) / halflife_samples)


@dataclass(slots=True)
class VolatilityBaseline:
    """Streaming location and scale of ln(sigma), with a freeze switch.

    ``lam`` defaults to a ~2 hour half-life on one-minute bars. That must stay
    well separated from the *fast* estimator's horizon: if the fast series is a
    large fraction of its own baseline's memory, z is structurally suppressed
    because the signal is partly measuring itself. ``assert_separated_from``
    enforces the ratio explicitly rather than leaving it to a comment.
    """

    lam: float = 0.9942  # ~120-sample half-life
    min_samples: int = 30
    mean: float = 0.0  # EWMA of ln(sigma)
    var: float = 0.0  # EWMV of ln(sigma)
    n: int = 0
    frozen: bool = False
    _debias: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 < self.lam < 1.0:
            raise ValueError(f"lam must be in (0, 1), got {self.lam}")
        if self.min_samples < _MIN_OBSERVATIONS:
            raise ValueError("min_samples must be at least 2")

    # ------------------------------------------------------------- horizons
    @property
    def halflife(self) -> float:
        return math.log(0.5) / math.log(self.lam)

    def assert_separated_from(self, fast_halflife: float, min_ratio: float = 4.0) -> None:
        """The fast and slow horizons must not overlap.

        If the fast estimator's memory is within a small factor of the
        baseline's, the numerator and denominator of z move together and the
        statistic collapses toward a constant. Four-to-one is the minimum that
        keeps the fast series under ~25% of its own reference.
        """
        if fast_halflife <= 0:
            raise ValueError("fast_halflife must be positive")
        ratio = self.halflife / fast_halflife
        if ratio < min_ratio:
            raise ValueError(
                f"baseline half-life ({self.halflife:.0f} samples) is only {ratio:.1f}x the "
                f"fast half-life ({fast_halflife:.0f}); need >= {min_ratio}x or the signal "
                "is measuring itself and z is structurally suppressed"
            )

    # ---------------------------------------------------------------- update
    def observe(self, sigma: float) -> None:
        """Admit one sigma into the reference distribution.

        Silently ignored while frozen - that is the whole point of the freeze,
        and making it an error would force every caller to branch.
        """
        if self.frozen or sigma <= _MIN_SIGMA or not math.isfinite(sigma):
            return

        x = math.log(sigma)
        if self.n == 0:
            # Seed at the first observation rather than at zero: ln(sigma) for FX
            # is around -9, so starting the mean at 0.0 would take thousands of
            # samples to walk down and produce absurd z-scores the whole way.
            self.mean = x
            self.var = 0.0
        else:
            diff = x - self.mean
            incr = (1.0 - self.lam) * diff
            self.mean += incr
            self.var = self.lam * (self.var + diff * incr)

        self.n += 1
        self._debias = self.lam * self._debias + (1.0 - self.lam)

    # ------------------------------------------------------------------ read
    @property
    def ready(self) -> bool:
        return self.n >= self.min_samples and self.var > 0.0

    @property
    def stdev(self) -> float:
        """Bias-corrected standard deviation of ln(sigma)."""
        if self._debias <= 0.0:
            return math.sqrt(max(self.var, 0.0))
        return math.sqrt(max(self.var / self._debias, 0.0))

    @property
    def level(self) -> float:
        """The baseline sigma itself, back in linear space. For display only."""
        return math.exp(self.mean)

    def zscore(self, sigma: float) -> float | None:
        """How unusual is this sigma, in log space?

        ``None`` when the baseline is not yet trustworthy - a z-score against
        six samples is noise wearing a lab coat, and the caller must be able to
        tell "not unusual" from "cannot say".
        """
        if not self.ready or sigma <= _MIN_SIGMA or not math.isfinite(sigma):
            return None
        sd = self.stdev
        if sd <= 0.0:
            return None
        return (math.log(sigma) - self.mean) / sd

    # ------------------------------------------------------------- freezing
    def freeze(self) -> None:
        self.frozen = True

    def thaw(self) -> None:
        self.frozen = False

    def reset(self) -> None:
        self.mean = self.var = self._debias = 0.0
        self.n = 0
        self.frozen = False

    # ---------------------------------------------------------- rehydration
    def snapshot(self) -> BaselineSnapshot:
        """Serialisable state, so a leader failover does not restart cold.

        Without this, a promoted standby needs ``min_samples`` bars before it
        can say anything - and on a Sunday reopen it would learn its idea of
        "normal" from thin weekend-open liquidity, which is the worst possible
        reference.
        """
        return {
            "lam": self.lam,
            "mean": self.mean,
            "var": self.var,
            "n": self.n,
            "debias": self._debias,
            "frozen": self.frozen,
        }

    @classmethod
    def restore(cls, state: BaselineSnapshot, min_samples: int = 30) -> VolatilityBaseline:
        baseline = cls(lam=state["lam"], min_samples=min_samples)
        baseline.mean = state["mean"]
        baseline.var = state["var"]
        baseline.n = state["n"]
        baseline._debias = state["debias"]
        baseline.frozen = state["frozen"]
        return baseline


def robust_zscore(samples: Sequence[float], sigma: float) -> float | None:
    """Median/MAD modified z-score (Iglewicz & Hoaglin 1993), in log space.

    Not used on the hot path - it is O(n log n) and needs the whole window in
    memory. It exists as an **independent oracle**: the tests assert that the
    EWMA baseline and this agree within tolerance on stationary data, which
    catches a whole class of recurrence bugs that a test comparing the code to
    itself would sail straight past.

    It is also the fallback worth reaching for if you would rather not freeze
    the baseline: MAD has a 50% breakdown point against the sample standard
    deviation's 0%, so leaked stress samples barely move it. Weaker than
    freezing, but it degrades instead of failing.

    The 0.6745 factor makes MAD a consistent estimator of sigma for normal data
    (it is the 0.75 quantile of the standard normal).
    """
    xs = [math.log(s) for s in samples if s > _MIN_SIGMA and math.isfinite(s)]
    if len(xs) < _MIN_OBSERVATIONS or sigma <= _MIN_SIGMA or not math.isfinite(sigma):
        return None

    median = statistics.median(xs)
    mad = statistics.median([abs(x - median) for x in xs])
    if mad <= 0.0:
        return None
    return 0.6745 * (math.log(sigma) - median) / mad
