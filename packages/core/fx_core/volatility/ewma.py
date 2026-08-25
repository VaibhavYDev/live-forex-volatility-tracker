"""Exponentially weighted moving variance (RiskMetrics).

    sigma^2_t = lambda * sigma^2_{t-1} + (1 - lambda) * r_t^2

O(1) per tick with *no window at all* - no ring buffer, no eviction, constant
memory per symbol. That is why it is the headline number: it survives a firehose.

On lambda
---------
0.94 is J.P. Morgan's RiskMetrics convention for *daily* returns. Blindly reusing
it on 1-minute bars gives an effective memory of ~16 bars, which is far too twitchy.
Effective memory is roughly ``1 / (1 - lambda)`` observations, so:

    lambda = 0.94  ->   ~17 bars   (~17 min on 1m bars)
    lambda = 0.97  ->   ~33 bars
    lambda = 0.99  ->  ~100 bars   (~1.7h on 1m bars)

``lambda_for_halflife`` derives it from a stated half-life instead, which is the
honest way to configure this: you pick the horizon you care about, not a number
copied from a 1996 paper about a different sampling frequency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["EwmaVariance", "lambda_for_halflife"]


def lambda_for_halflife(halflife_bars: float) -> float:
    """Decay factor whose weight halves after ``halflife_bars`` observations."""
    if halflife_bars <= 0:
        raise ValueError("halflife_bars must be positive")
    return math.exp(-math.log(2.0) / halflife_bars)


@dataclass(slots=True)
class EwmaVariance:
    lam: float = 0.97
    variance: float = 0.0
    n: int = 0
    _debias: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 < self.lam < 1.0:
            raise ValueError(f"lam must be in (0, 1), got {self.lam}")

    def update(self, ret: float) -> float:
        """Feed one log return, get the updated variance."""
        if not math.isfinite(ret):
            # A single NaN would permanently poison the recursion - there is no
            # window to age it out of. Drop it; the caller counts it.
            return self.variance

        if self.n == 0:
            # Seeding with r^2 rather than 0 avoids a long warm-up ramp where the
            # reported sigma is meaninglessly small.
            self.variance = ret * ret
        else:
            self.variance = self.lam * self.variance + (1.0 - self.lam) * ret * ret

        self.n += 1
        self._debias = self.lam * self._debias + (1.0 - self.lam)
        return self.variance

    @property
    def sigma(self) -> float:
        return math.sqrt(max(self.variance, 0.0))

    @property
    def bias_corrected_variance(self) -> float:
        """Adam-style correction for the early-sample downward bias."""
        if self._debias <= 0.0:
            return self.variance
        return self.variance / self._debias

    @property
    def bias_corrected_sigma(self) -> float:
        return math.sqrt(max(self.bias_corrected_variance, 0.0))

    @property
    def warmed_up(self) -> bool:
        """True once the estimate is worth displaying.

        Serving a sigma computed from three ticks as if it were a real reading is
        how dashboards lie. The UI greys the value out until this flips.
        """
        return self.n >= max(int(1.0 / (1.0 - self.lam)), 2)

    @property
    def effective_memory(self) -> float:
        return 1.0 / (1.0 - self.lam)

    def reset(self) -> None:
        self.variance = 0.0
        self.n = 0
        self._debias = 0.0
