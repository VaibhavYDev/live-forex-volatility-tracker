"""A tick generator whose per-minute realised volatility is exactly what we say.

Why the returns are sign-randomised rather than Gaussian
--------------------------------------------------------
The obvious generator draws each intra-bar return from ``N(0, sigma/sqrt(k))``,
which gives a bar whose realised volatility is only *approximately* sigma: the
estimator ``sqrt(sum r^2)`` over k returns has a coefficient of variation of
``sqrt(2/k)`` - 58% at k=6, 32% at k=20. That sampling error would dominate the
noise we are deliberately injecting, and the test would then be measuring the
variance of the estimator instead of the behaviour of the detector.

So each return has a FIXED magnitude ``sigma/sqrt(k)`` and a random sign. The
path is still a random walk (so OHLC is realistic), but ``sum r^2`` is exactly
``sigma^2`` by construction. Every bit of noise in the test is noise we chose,
at an amplitude we stated.

The macro signal
----------------
    sigma(t) = base * exp(A * sin(2*pi*t/period)) * exp(N(0, noise_sd))

Multiplicative, because volatility scales multiplicatively - which is also why
the detector works in log space. In logs this is a clean sine plus Gaussian
noise, so "amplitude 1.2 with noise 0.4" means exactly what it sounds like.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fx_core.models import Tick

__all__ = ["SineVolatilityFeed", "macro_phase"]


def macro_phase(minute: int, warmup: int, period: int) -> float:
    """Position within the macro cycle, 0.0 to 1.0. Crest is at 0.25."""
    return ((minute - warmup) % period) / period


@dataclass
class SineVolatilityFeed:
    symbol: str = "EURUSD"
    base_sigma: float = 1e-4
    amplitude: float = 1.2  # in log space: crest/trough ratio is exp(2*A) ~ 11x
    noise_sd: float = 0.40  # heavy: a third of the macro amplitude
    period_minutes: int = 180
    warmup_minutes: int = 150  # let the baseline reach min_samples before the cycle starts
    ticks_per_minute: int = 8
    start: datetime = datetime(2026, 8, 19, 0, 0, tzinfo=UTC)  # a Wednesday: market open
    seed: int = 3
    start_price: float = 1.0842

    def target_sigma(self, minute: int) -> float:
        """The realised volatility this minute's bar will have, before noise."""
        if minute < self.warmup_minutes:
            return self.base_sigma
        phase = (minute - self.warmup_minutes) / self.period_minutes
        return self.base_sigma * math.exp(self.amplitude * math.sin(2 * math.pi * phase))

    def ticks(self, minutes: int) -> Iterator[Tick]:
        """Yield ticks minute by minute, in order.

        Note the pipeline seals a bar on the arrival of the NEXT minute's first
        tick, not on a timer - so driving N minutes produces N-1 sealed bars.
        """
        rng = random.Random(self.seed)
        price = self.start_price
        seq = 0

        for minute in range(minutes):
            sigma = self.target_sigma(minute) * math.exp(rng.gauss(0.0, self.noise_sd))
            step = sigma / math.sqrt(self.ticks_per_minute)
            minute_start = self.start + timedelta(minutes=minute)

            for k in range(self.ticks_per_minute):
                price *= math.exp(step if rng.random() < 0.5 else -step)
                seq += 1
                ts = minute_start + timedelta(seconds=(k + 1) * 60.0 / (self.ticks_per_minute + 1))
                half = price * 5e-5
                yield Tick(
                    symbol=self.symbol,
                    bid=price - half,
                    ask=price + half,
                    ts_event=ts,
                    ts_ingest=ts,
                    seq=seq,
                )
