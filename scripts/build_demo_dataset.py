"""Bake a demo session using the real engine.

WHY THIS EXISTS
---------------
The published dashboard has to answer a link that anyone can click, at any
hour, with nothing running. A static host runs no Python, so the volatility has
to be computed BEFORE it is published rather than while it is being watched.

The alternative — reimplementing Welford, EWMA, the four range estimators and
the hysteresis state machine in TypeScript so the browser could compute them —
would put a second implementation of the numerical core in the repository. Two
implementations of the same statistics disagree eventually, and the one on the
public URL would be the one nobody tests.

So this runs the ACTUAL modules the server runs:

    fx_core.backfill.synth_bars     the same generator the ingestor backfills with
    fx_core.volatility.*            Welford, EWMA, Parkinson, Garman-Klass,
                                    Rogers-Satchell, Yang-Zhang
    fx_core.alerts.RegimeDetector   the log-space baseline and Schmitt trigger
    fx_core.intervals.fold          the same roll-up the API serves

Every figure on the published page came out of those. The browser only draws.

    python scripts/build_demo_dataset.py apps/web/src/demo/dataset.json
"""

from __future__ import annotations

import json
import math
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fx_core.alerts import DetectorConfig, RegimeDetector, TriggerConfig
from fx_core.backfill import synth_bars
from fx_core.intervals import ORDER, fold
from fx_core.models import Bar, BarSource
from fx_core.volatility import (
    EwmaVariance,
    close_to_close,
    garman_klass,
    parkinson,
    rogers_satchell,
    yang_zhang,
)

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"]

# (price, annualised vol, pip) — mirrors the replay provider's specs so the
# published demo and a locally-run stack show the same pairs at the same levels.
SPECS: dict[str, tuple[float, float, float]] = {
    "EURUSD": (1.0850, 0.070, 0.0001),
    "GBPUSD": (1.2720, 0.085, 0.0001),
    "USDJPY": (147.20, 0.095, 0.01),
    "AUDUSD": (0.6620, 0.100, 0.0001),
    "USDCHF": (0.8730, 0.072, 0.0001),
}

#: Candles shipped per timeframe. 300 fills a wide chart with room to pan, and
#: keeps the whole dataset small enough to serve as one static file.
PER_TF = 300

#: The 1-minute series length is the sum of the episode lengths below - roughly
#: eight days, which is what 4h x 300 candles needs.

#: Hourly bars behind 1h and 4h. 4h x 300 candles is 50 days, which the minute
#: series cannot reach without shipping 72_000 bars — but base series are used
#: only at BUILD time (the file carries the folded output), so a third base
#: costs nothing on the wire.
HOURS = 2 * 365 * 24

#: Daily bars behind 1d/1w/1M. 300 monthly candles is 25 years.
DAYS = 25 * 365

#: Ticks the browser loops to keep the price moving. One minute of feed at a
#: watchable rate — long enough not to read as a loop, small enough to inline.
TICKS = 900
TICK_HZ = 6.0

SECONDS_PER_TRADING_YEAR = 252 * 24 * 3600


def to_bar(symbol: str, raw: dict[str, Any], prev_close: float | None) -> Bar:
    """Lift a generated OHLC dict into the model the estimators consume.

    ``sum_ret`` / ``sum_ret_sq`` are the additive statistics the real pipeline
    accumulates per tick. Reconstructing them from the bar's own log return
    keeps close-to-close and the EWMA consistent with the candles drawn beside
    them, which is the whole point of computing this here rather than in JS.
    """
    ret = 0.0 if prev_close is None else math.log(raw["c"] / prev_close)
    return Bar(
        symbol=symbol,
        bucket=datetime.fromtimestamp(raw["t"], tz=UTC),
        open=raw["o"],
        high=raw["h"],
        low=raw["l"],
        close=raw["c"],
        tick_count=raw["n"],
        sum_ret=ret,
        sum_ret_sq=ret * ret,
        source=BarSource.SYNTHETIC,
    )


def estimators_over(bars: list[Bar]) -> dict[str, float]:
    """The five annualised figures the side panel prints, same window."""
    scale = math.sqrt(SECONDS_PER_TRADING_YEAR / 60)
    return {
        "close_to_close": close_to_close(bars) * scale,
        "parkinson": parkinson(bars) * scale,
        "garman_klass": garman_klass(bars) * scale,
        "rogers_satchell": rogers_satchell(bars) * scale,
        "yang_zhang": yang_zhang(bars) * scale,
    }


def run_detector(symbol: str, bars: list[Bar]) -> tuple[list[dict], list[dict], Any]:
    """Drive the real RegimeDetector bar by bar.

    Returns the z-history the pane draws, the transitions the toast stack shows,
    and the detector itself so its live thresholds can be published alongside —
    the band drawn on screen must be the band this detector actually used.
    """
    detector = RegimeDetector(
        symbol=symbol, config=DetectorConfig(), trigger_config=TriggerConfig()
    )
    ewma = EwmaVariance(lam=0.97)
    zhist: list[dict] = []
    alerts: list[dict] = []

    for bar in bars:
        ewma.update(bar.sum_ret)
        sigma = math.sqrt(max(ewma.variance, 0.0))
        if sigma <= 0.0:
            continue

        transition = detector.update(sigma, bar.bucket)
        z = detector.last_zscore
        if z is not None and math.isfinite(z):
            zhist.append({"t": int(bar.bucket.timestamp()), "z": z, "r": str(detector.regime)})
        if transition is not None:
            alerts.append(
                {
                    "s": symbol,
                    "seq": len(alerts) + 1,
                    "ts": bar.bucket.isoformat(),
                    "old_regime": str(transition.old_regime),
                    "new_regime": str(transition.new_regime),
                    "trigger_value": transition.trigger_value,
                    "threshold_value": transition.threshold_value,
                    "sigma": transition.sigma,
                    "cause": str(transition.cause),
                    "reason": transition.reason,
                }
            )

    return zhist, alerts, (detector, ewma)


def tick_stream(symbol: str, last_close: float, spec: tuple[float, float, float]) -> list[dict]:
    """A loopable minute of quotes.

    Ends where it began so the loop seam is invisible: a walk that drifted would
    step the price every time the demo wrapped, which reads as a data glitch on
    a page nobody is maintaining.
    """
    _price, vol, pip = spec
    rng = random.Random(f"ticks:{symbol}")
    sigma = vol * math.sqrt((1.0 / TICK_HZ) / SECONDS_PER_TRADING_YEAR)

    steps = [rng.gauss(0.0, sigma) for _ in range(TICKS)]
    drift = sum(steps) / TICKS
    price = last_close
    out: list[dict] = []
    for i, step in enumerate(steps):
        price *= math.exp(step - drift)  # de-drifted: returns to the start
        half = pip * (0.3 + rng.random() * 0.5)
        out.append(
            {
                "i": i,
                "bid": round(price - half, 6),
                "ask": round(price + half, 6),
                "mid": round(price, 6),
            }
        )
    return out


#: Calm stretches punctuated by bursts, rather than one flat volatility.
#:
#: A constant-sigma series produces a z-score pinned near zero forever, so the
#: detector never fires: the published demo would show the hysteresis band, the
#: timeline strip and the toast stack, and never a single transition. The one
#: feature the project is named after would be invisible on its own front page.
#:
#: Clustering is also what volatility actually does (Mandelbrot 1963) - quiet
#: periods and violent ones arrive in runs, which is the entire reason a
#: trailing baseline is the right comparison.
#: (bars, multiple of the symbol's base vol)
_EPISODES: tuple[tuple[int, float], ...] = (
    (5_400, 1.0),
    (90, 4.2),  # a burst, well past enter_z
    (2_600, 1.0),
    (55, 3.4),
    (1_900, 0.6),  # unusually quiet, so the baseline is not a straight line
    (70, 3.8),
    (400, 1.0),  # calm again by the time the chart's right edge arrives
)


def clustered_minutes(symbol: str, now: int) -> list[dict[str, Any]]:
    """Chain per-episode walks into one continuous minute series.

    Built newest-first: each segment is generated ending at the price the
    following segment opens on, so the joins carry no gap. Generating forwards
    and rescaling afterwards would break OHLC coherence - a high can stop being
    the highest - and the range estimators read exactly that.
    """
    price, vol, pip = SPECS[symbol]
    end = now
    close = price
    chunks: list[list[dict[str, Any]]] = []

    for i, (count, mult) in enumerate(reversed(_EPISODES)):
        seg = synth_bars(
            end_epoch=end,
            count=count,
            seconds=60,
            price=close,
            annual_vol=vol * mult,
            pip=pip,
            seed=42 + i,
        )
        if not seg:
            continue
        chunks.append(seg)
        # The next (earlier) segment must land on this one's opening price.
        close = seg[0]["o"]
        end = seg[0]["t"] - 60

    out: list[dict[str, Any]] = []
    for seg in reversed(chunks):
        out.extend(seg)
    return out


def build_symbol(symbol: str, now: int) -> dict[str, Any]:
    price, vol, pip = SPECS[symbol]

    minutes = clustered_minutes(symbol, now)
    hourly = synth_bars(
        end_epoch=now,
        count=HOURS,
        seconds=3_600,
        price=price,
        annual_vol=vol,
        pip=pip,
        seed=42,
    )
    daily = synth_bars(
        end_epoch=now,
        count=DAYS,
        seconds=86_400,
        price=price,
        annual_vol=vol,
        pip=pip,
        seed=42,
    )

    # Every timeframe pre-folded with the SAME function the API uses, so the
    # published chart and a local one agree candle for candle.
    # Which base each timeframe folds from. Same principle the API uses — read
    # from the coarsest series that still resolves the requested bucket, because
    # folding 1M from minutes would need two million bars to draw 300 candles.
    bases = {
        "1m": minutes,
        "5m": minutes,
        "15m": minutes,
        "30m": minutes,
        "1h": hourly,
        "4h": hourly,
        "1d": daily,
        "1w": daily,
        "1M": daily,
    }
    series: dict[str, list[dict]] = {code: fold(bases[code], code)[-PER_TF:] for code in ORDER}

    # Statistics run over the 1-minute series, which is what the panel claims.
    models: list[Bar] = []
    prev: float | None = None
    for raw in minutes[-1_500:]:
        models.append(to_bar(symbol, raw, prev))
        prev = raw["c"]

    zhist, alerts, (detector, _ewma) = run_detector(symbol, models)
    window = models[-120:]

    return {
        "series": series,
        "estimators": estimators_over(window),
        "bars_in_window": len(window),
        "zhist": zhist[-PER_TF:],
        "alerts": alerts[-40:],
        "enter_z": detector.trigger.config.enter_z,
        "exit_z": detector.trigger.config.exit_z,
        "regime": str(detector.regime),
        "sigma_ann": estimators_over(window)["close_to_close"],
        "ticks": tick_stream(symbol, minutes[-1]["c"], SPECS[symbol]),
    }


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "apps/web/src/demo/dataset.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    # Anchored to a fixed instant, not to "now". A dataset rebuilt on every CI
    # run would rewrite every timestamp in the file and make the diff useless;
    # the browser rebases these onto the viewer's clock at load time.
    anchor = int(datetime(2026, 1, 5, 12, 0, tzinfo=UTC).timestamp())

    # No wall-clock field anywhere in the output. The generator is deterministic,
    # so the committed dataset and the one CI rebuilds are byte-identical — which
    # turns the CI rebuild into a free check that the engine still produces the
    # same numbers, instead of a source of silent divergence from the file a
    # reviewer can actually read in the repo.
    data = {
        "anchor": anchor,
        "tick_hz": TICK_HZ,
        "symbols": SYMBOLS,
        "by_symbol": {s: build_symbol(s, anchor) for s in SYMBOLS},
    }

    out.write_text(json.dumps(data, separators=(",", ":")))
    kb = out.stat().st_size / 1024
    print(f"{out}: {kb:.0f} kB")
    for s in SYMBOLS:
        d = data["by_symbol"][s]
        print(
            f"  {s}: {len(d['zhist'])} z, {len(d['alerts'])} alerts, "
            f"{sum(len(v) for v in d['series'].values())} candles"
        )


if __name__ == "__main__":
    main()
