"""Deterministic synthetic history.

WHY THIS EXISTS
---------------
A freshly deployed demo has no past. Without it the chart shows however many
minutes the container has been up, every timeframe above 1h is empty, and the
z-score pane stays blank for the first half hour because the detector's baseline
has nothing to learn from. A reviewer opening the link sixty seconds after a
deploy sees an empty product.

So on first boot we synthesise the history the feed would have produced had it
been running. This is not a claim about the market: the live feed is itself the
deterministic `replay` provider, and this generates the same process over past
timestamps with the same seed. It is labelled `src: "backfill"` on every bar so
nothing downstream can mistake it for observed data.

THE MODEL
---------
Geometric Brownian motion, with per-bar volatility scaled from the symbol's
annualised figure by sqrt(dt) — variance is additive in time, so standard
deviation grows with the square root of it. Using the same 252x24h annualisation
basis as fx_core.models means a Parkinson or Yang-Zhang estimate computed over
these bars lands near the symbol's stated vol instead of contradicting it.

The walk runs BACKWARDS from the symbol's reference price so the most recent
synthetic close sits where the live feed is about to begin. Generating forwards
would leave a discontinuity at the join — a gap candle at exactly the moment the
demo starts, which is the first thing anyone would notice.
"""

from __future__ import annotations

import math
import random
from typing import Any, Final

#: Must match fx_core.models and the replay provider, or the estimators printed
#: beside the chart will disagree with the bars they were computed from.
SECONDS_PER_TRADING_YEAR: Final = 252 * 24 * 3600

#: Wick size as a fraction of the bar's own move. Real bars overshoot their
#: open/close range; candles drawn without wicks look synthetic at a glance.
_WICK: Final = 0.6


def synth_bars(
    *,
    end_epoch: int,
    count: int,
    seconds: int,
    price: float,
    annual_vol: float,
    pip: float,
    seed: int,
) -> list[dict[str, Any]]:
    """`count` bars of `seconds` each, ending at the bucket containing `end_epoch`.

    Deterministic in (seed, count, seconds, price): two replicas backfilling the
    same symbol produce byte-identical bars, so a standby promoting mid-demo
    cannot redraw the chart.
    """
    if count <= 0:
        return []

    rng = random.Random(f"{seed}:{seconds}:{price}")
    sigma = annual_vol * math.sqrt(seconds / SECONDS_PER_TRADING_YEAR)

    last_start = end_epoch - (end_epoch % seconds)
    closes: list[float] = [price]
    # Backwards: today's close is known, so each earlier close is derived by
    # undoing one step of the walk.
    for _ in range(count):
        prev = closes[-1] / math.exp(rng.gauss(0.0, sigma))
        closes.append(prev)
    closes.reverse()

    bars: list[dict[str, Any]] = []
    for i in range(count):
        o, c = closes[i], closes[i + 1]
        span = abs(c - o)
        # A flat bar still has a range; without this, quiet periods render as
        # zero-height candles and every range estimator divides toward zero.
        floor = pip * 2
        hi = max(o, c) + max(span * _WICK * rng.random(), floor * rng.random())
        lo = min(o, c) - max(span * _WICK * rng.random(), floor * rng.random())
        bars.append(
            {
                "t": last_start - (count - 1 - i) * seconds,
                "o": round(o, 6),
                "h": round(hi, 6),
                "l": round(lo, 6),
                "c": round(c, 6),
                # Plausible activity, and non-zero so anything weighting by tick
                # count does not treat backfilled bars as empty.
                "n": rng.randint(seconds // 4 or 1, max(seconds, 2)),
                "src": "backfill",
            }
        )
    return bars
