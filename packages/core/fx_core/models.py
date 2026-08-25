"""Domain models.

Deliberately stdlib-only frozen slotted dataclasses rather than pydantic:

* ``fx_core`` must stay dependency-free so its tests run in milliseconds and a
  contributor can reason about the maths without touching the streaming layer.
* These sit on the hot path (tens of ticks/sec/symbol). Slots remove per-instance
  ``__dict__`` allocation; frozen makes them safe to hand to multiple consumers.

Validation happens once, at the provider adapter boundary (``fx_ingestor.providers``),
so nothing downstream ever sees a provider-shaped payload. Pydantic schemas live in
``fx_api.schemas`` where they belong: the HTTP/WS edge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

__all__ = [
    "Bar",
    "BarSource",
    "Estimator",
    "Tick",
    "VolSnapshot",
    "bars_per_year",
]

_MIN_SAMPLES = 2


class BarSource(StrEnum):
    """Provenance. Never let a chart imply that backfilled data arrived live."""

    STREAM = "stream"
    BACKFILL = "backfill"
    SYNTHETIC = "synthetic"


class Estimator(StrEnum):
    CLOSE_TO_CLOSE = "close_to_close"
    EWMA = "ewma"
    PARKINSON = "parkinson"
    GARMAN_KLASS = "garman_klass"
    ROGERS_SATCHELL = "rogers_satchell"
    YANG_ZHANG = "yang_zhang"


@dataclass(frozen=True, slots=True)
class Tick:
    """A single top-of-book update, normalised across providers."""

    symbol: str
    bid: float
    ask: float
    ts_event: datetime
    ts_ingest: datetime
    seq: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def ingest_lag_s(self) -> float:
        """Provider timestamp -> our process. The headline latency metric."""
        return (self.ts_ingest - self.ts_event).total_seconds()

    def validate(self) -> None:
        """Raise on anything structurally impossible.

        Called once at the adapter boundary. A crossed book or a non-positive price
        means the provider frame is corrupt; we drop it and increment a counter
        rather than letting NaN propagate into the variance state, where it would
        poison every downstream number permanently.
        """
        if not self.symbol:
            raise ValueError("empty symbol")
        if not (math.isfinite(self.bid) and math.isfinite(self.ask)):
            raise ValueError(f"non-finite price: bid={self.bid} ask={self.ask}")
        if self.bid <= 0 or self.ask <= 0:
            raise ValueError(f"non-positive price: bid={self.bid} ask={self.ask}")
        if self.ask < self.bid:
            raise ValueError(f"crossed book: bid={self.bid} > ask={self.ask}")
        if self.ts_event.tzinfo is None:
            raise ValueError("ts_event must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Bar:
    """A time bucket carrying OHLC *and* additive sufficient statistics.

    ``sum_ret`` / ``sum_ret_sq`` are what make longer windows cheap: a 1h variance
    is the sum of sixty 1m bars' statistics, so we never revisit ticks to widen a
    window. This is why the rollup job is pure addition.
    """

    symbol: str
    bucket: datetime
    open: float
    high: float
    low: float
    close: float
    tick_count: int
    sum_ret: float
    sum_ret_sq: float
    source: BarSource = BarSource.STREAM

    def merge(self, other: Bar) -> Bar:
        """Combine two adjacent bars of the same symbol into a wider one."""
        if self.symbol != other.symbol:
            raise ValueError(f"cannot merge {self.symbol} with {other.symbol}")
        earlier, later = (self, other) if self.bucket <= other.bucket else (other, self)
        return replace(
            earlier,
            open=earlier.open,
            high=max(self.high, other.high),
            low=min(self.low, other.low),
            close=later.close,
            tick_count=self.tick_count + other.tick_count,
            sum_ret=self.sum_ret + other.sum_ret,
            sum_ret_sq=self.sum_ret_sq + other.sum_ret_sq,
            source=(
                BarSource.BACKFILL
                if BarSource.BACKFILL in (self.source, other.source)
                else self.source
            ),
        )

    @property
    def variance(self) -> float:
        """Sample variance of log returns, from the stored statistics alone."""
        n = self.tick_count
        if n < _MIN_SAMPLES:
            return 0.0
        mean = self.sum_ret / n
        var = (self.sum_ret_sq - n * mean * mean) / (n - 1)
        return max(var, 0.0)  # guard the tiny negative from float cancellation

    @property
    def realized_vol(self) -> float:
        """Realised volatility over this bar: sqrt of the summed squared returns.

        The standard estimator of integrated variance over an interval (Andersen
        & Bollerslev 1998). Note it is NOT ``sqrt(variance)``: that would be the
        per-tick dispersion, which shrinks as ticks get more frequent. This is
        the volatility *of the minute*, which is what a regime detector wants -
        and it is broadly invariant to tick rate, so a busy minute and a quiet
        minute with the same price path score the same.

        Returns 0.0 for a bar with fewer than two ticks, which has no returns to
        square. Downstream, a zero sigma is treated as unmeasurable rather than
        as "perfectly calm" - see ``VolatilityBaseline.observe``.
        """
        return math.sqrt(max(self.sum_ret_sq, 0.0))


@dataclass(frozen=True, slots=True)
class VolSnapshot:
    """A volatility reading, always carrying its own provenance.

    An unlabelled sigma is meaningless: 0.004 could be per-tick, per-minute or
    annualised, close-to-close or Parkinson. Every number we render is accompanied
    by the estimator, the window and the annualisation basis. A finance-literate
    reviewer checks for exactly this.
    """

    symbol: str
    ts: datetime
    estimator: Estimator
    window_s: int
    sigma: float
    sigma_annualized: float
    sample_count: int
    zscore: float | None = None

    @classmethod
    def build(
        cls,
        symbol: str,
        estimator: Estimator,
        window_s: int,
        sigma: float,
        sample_count: int,
        bar_seconds: int,
        ts: datetime | None = None,
        zscore: float | None = None,
    ) -> Self:
        return cls(
            symbol=symbol,
            ts=ts or datetime.now(UTC),
            estimator=estimator,
            window_s=window_s,
            sigma=sigma,
            sigma_annualized=sigma * math.sqrt(bars_per_year(bar_seconds)),
            sample_count=sample_count,
            zscore=zscore,
        )


# FX trades ~24h a day, 5 days a week. The market convention is 252 trading days,
# so a year contains 252 * 24 = 6_048 hours of trading -> 362_880 one-minute bars.
# Stated explicitly because "annualised" means nothing without the basis.
TRADING_DAYS_PER_YEAR = 252
TRADING_HOURS_PER_DAY = 24
SECONDS_PER_TRADING_YEAR = TRADING_DAYS_PER_YEAR * TRADING_HOURS_PER_DAY * 3600


def bars_per_year(bar_seconds: int) -> float:
    """Number of bars of ``bar_seconds`` length in one FX trading year."""
    if bar_seconds <= 0:
        raise ValueError("bar_seconds must be positive")
    return SECONDS_PER_TRADING_YEAR / bar_seconds
