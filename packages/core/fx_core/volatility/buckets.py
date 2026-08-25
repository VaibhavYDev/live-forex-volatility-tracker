"""Additive time buckets - how a sliding window stays cheap.

The problem
-----------
Welford is O(1) to *add* a point but cannot cheaply *remove* one: the subtraction
form is numerically unstable and loses the guarantee that makes Welford worth using.
So a naive sliding window recomputes over its contents on every tick: O(n) per tick,
which is the shape that melts under a firehose.

The fix
-------
Bucket by minute and keep *sufficient statistics* per bucket - ``(count, sum,
sum_sq, o, h, l, c)``. These are **additive**, so a 5m / 15m / 1h window is
"sum the last K buckets, drop the oldest": O(K) with K <= 60, independent of tick
rate. Ticks per bucket can be 1 or 10_000; the window cost does not change.

This is the sub-window aggregation trick behind Exponential Histograms and DGIM
(Datar-Gionis-Indyk-Motwani, 2002). We use the fixed-width variant because our
windows are wall-clock aligned anyway, which the general algorithm does not assume.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fx_core.models import Bar, BarSource
from fx_core.volatility.welford import Welford

__all__ = ["BucketAccumulator", "RollingWindow"]


def floor_to_bucket(ts: datetime, bucket_seconds: int) -> datetime:
    """Align a timestamp down to its bucket boundary."""
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % bucket_seconds), tz=UTC)


@dataclass(slots=True)
class BucketAccumulator:
    """One in-progress time bucket. Mutable by design; sealed into a frozen Bar."""

    symbol: str
    bucket: datetime
    open: float
    high: float
    low: float
    close: float
    tick_count: int = 0
    sum_ret: float = 0.0
    sum_ret_sq: float = 0.0
    source: BarSource = BarSource.STREAM

    @classmethod
    def start(
        cls,
        symbol: str,
        ts: datetime,
        price: float,
        bucket_seconds: int,
        source: BarSource = BarSource.STREAM,
    ) -> BucketAccumulator:
        return cls(
            symbol=symbol,
            bucket=floor_to_bucket(ts, bucket_seconds),
            open=price,
            high=price,
            low=price,
            close=price,
            source=source,
        )

    def add(self, price: float, prev_price: float | None) -> None:
        """Fold one tick in. Returns nothing; this is the hot path."""
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.tick_count += 1

        if prev_price is not None and prev_price > 0.0 and price > 0.0:
            r = math.log(price / prev_price)
            self.sum_ret += r
            self.sum_ret_sq += r * r

    def seal(self) -> Bar:
        return Bar(
            symbol=self.symbol,
            bucket=self.bucket,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            tick_count=self.tick_count,
            sum_ret=self.sum_ret,
            sum_ret_sq=self.sum_ret_sq,
            source=self.source,
        )


@dataclass(slots=True)
class RollingWindow:
    """A time-bounded deque of sealed bars with O(1) amortised eviction.

    ``window_seconds`` is wall-clock, not a bar count, so the window stays correct
    across a quiet period where fewer bars were produced - which matters because
    FX genuinely goes quiet (Asian session lunch, holidays) and a count-based
    window would silently reach hours back.
    """

    window_seconds: int
    bars: deque[Bar] = field(default_factory=deque)

    def push(self, bar: Bar) -> None:
        self.bars.append(bar)
        self.evict(bar.bucket)

    def evict(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.window_seconds)
        while self.bars and self.bars[0].bucket < cutoff:
            self.bars.popleft()

    def __len__(self) -> int:
        return len(self.bars)

    def as_list(self) -> list[Bar]:
        return list(self.bars)

    @property
    def tick_count(self) -> int:
        return sum(b.tick_count for b in self.bars)

    def welford(self) -> Welford:
        """Combine the window's bars into a single accumulator.

        O(number of bars), not O(number of ticks). With 1m bars and a 1h window
        that is 60 merges regardless of whether 600 or 600_000 ticks arrived.
        """
        acc = Welford()
        for bar in self.bars:
            acc = acc.merge(Welford.from_sums(bar.tick_count, bar.sum_ret, bar.sum_ret_sq))
        return acc

    def sigma(self) -> float:
        """Realised volatility per tick over the window."""
        return self.welford().stdev

    def collapse(self) -> Bar | None:
        """Merge the whole window into one wide bar (for coarser estimators)."""
        if not self.bars:
            return None
        out = self.bars[0]
        for bar in list(self.bars)[1:]:
            out = out.merge(bar)
        return out
