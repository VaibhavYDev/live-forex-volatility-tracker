"""Timeframe roll-up.

WHY TWO BASE SERIES
-------------------
A chart showing 200 candles at 1-week resolution needs four years of history.
At 1-minute resolution four years is 2.1 million bars — which is both absurd to
store and unrenderable: at a 950px chart that is 0.0004 px per candle.

So intraday and long-range read from different stored series, which is what real
vendors do rather than a compromise around them. Tiingo and Polygon will sell you
minute bars for recent weeks and daily bars for decades, for exactly this reason:

    1m  5m  15m  30m  1h  4h   <- folded from the stored 1-minute series
    1d  1w  1M                 <- folded from the stored daily series

Every fixed interval below divides evenly into 86_400, so buckets align to
midnight UTC and never drift. Weeks and months cannot work that way — a month is
not a number of seconds — so those two bucket by calendar date instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

Base = Literal["1m", "1d"]


@dataclass(frozen=True, slots=True)
class Interval:
    code: str
    #: None for calendar intervals, whose length is not a constant.
    seconds: int | None
    base: Base


INTERVALS: Final[dict[str, Interval]] = {
    "1m": Interval("1m", 60, "1m"),
    "5m": Interval("5m", 300, "1m"),
    "15m": Interval("15m", 900, "1m"),
    "30m": Interval("30m", 1_800, "1m"),
    "1h": Interval("1h", 3_600, "1m"),
    "4h": Interval("4h", 14_400, "1m"),
    "1d": Interval("1d", 86_400, "1d"),
    "1w": Interval("1w", None, "1d"),
    "1M": Interval("1M", None, "1d"),
}

#: Ordered for the UI. Dict order is insertion order, but relying on that for a
#: user-visible control is the kind of coupling that breaks on a refactor.
ORDER: Final[tuple[str, ...]] = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")


def bucket_start(epoch: int, interval: str) -> int:
    """Left edge of the bucket `epoch` belongs to.

    Fixed intervals use modular arithmetic. Calendar intervals resolve through
    `datetime`, because "the Monday of this week" and "the 1st of this month" are
    not expressible as an offset — a month is 28 to 31 days and arithmetic on a
    fixed 30 silently drifts a day every couple of months.
    """
    spec = INTERVALS[interval]
    if spec.seconds is not None:
        return epoch - (epoch % spec.seconds)

    at = datetime.fromtimestamp(epoch, tz=UTC)
    if interval == "1w":
        # ISO weeks start Monday. weekday() is 0 on Monday, so this lands on the
        # most recent Monday midnight rather than a rolling 7-day offset.
        monday = (at - timedelta(days=at.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return int(monday.timestamp())
    first = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first.timestamp())


def fold(bars: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    """Roll base bars up into `interval` buckets.

    OHLC does not average. Open is the FIRST open in the bucket and close the
    LAST close, so both depend on ordering; high and low are extrema, and tick
    count sums. Getting open/close backwards produces a chart that looks
    plausible and is wrong, which is the worst kind of wrong.

    Input must be ascending by `t`. Partial trailing buckets are returned — the
    current hour is legitimately incomplete and the chart should show it forming.
    """
    if interval not in INTERVALS:
        raise ValueError(f"unknown interval: {interval}")
    if not bars:
        return []

    out: list[dict[str, Any]] = []
    for bar in sorted(bars, key=lambda b: int(b["t"])):
        start = bucket_start(int(bar["t"]), interval)
        if out and out[-1]["t"] == start:
            cur = out[-1]
            cur["h"] = max(cur["h"], bar["h"])
            cur["l"] = min(cur["l"], bar["l"])
            cur["c"] = bar["c"]  # last close wins
            cur["n"] += bar.get("n", 0)
        else:
            out.append(
                {
                    "t": start,
                    "o": bar["o"],  # first open wins
                    "h": bar["h"],
                    "l": bar["l"],
                    "c": bar["c"],
                    "n": bar.get("n", 0),
                    "src": bar.get("src", "agg"),
                }
            )
    return out


#: Bar duration of each stored base series.
BASE_SECONDS: Final[dict[str, int]] = {"1m": 60, "1d": 86_400}

#: Upper bound on base bars per calendar bucket. Months are 28-31 days, so
#: reading 31 per month over-fetches slightly rather than returning a short
#: chart in February — the fold discards the surplus.
_CALENDAR_SPAN: Final[dict[str, int]] = {"1w": 7, "1M": 31}


def base_count(interval: str, limit: int) -> int:
    """How many base bars must be read to fold into `limit` bars of `interval`.

    Over-fetches by one bucket. The newest base bar rarely lands exactly on a
    bucket boundary, so an exact count loses the oldest candle to a partial
    fold and the chart silently comes back one short of what was asked for.
    """
    spec = INTERVALS[interval]
    per = (
        max(spec.seconds // BASE_SECONDS[spec.base], 1)
        if spec.seconds is not None
        else _CALENDAR_SPAN[interval]
    )
    return limit * per + per
