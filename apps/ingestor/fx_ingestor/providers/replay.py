"""Deterministic synthetic feed - the reason ``docker compose up`` needs no API key.

This is not a toy. It is load-bearing infrastructure:

* A reviewer clones the repo and has a live-looking dashboard in 60 seconds with
  no signup. If they cannot run it, none of the architecture matters.
* CI can assert numerical results, because a fixed seed makes the tick sequence
  byte-identical across runs.
* The chaos and load suites need a feed they can stall, corrupt and replay at
  100x on demand. You cannot ask Tiingo to do that.

The price process is a geometric Brownian motion per symbol with realistic
per-pair volatility, correct pip sizes (JPY pairs quote to 3dp, everything else
to 5dp), spreads that *widen when volatility rises* - as real spreads do - and
periodic volatility bursts so the alerting path has something to fire on.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from fx_core.models import Tick

from fx_ingestor.providers.base import MarketDataProvider

__all__ = ["SYMBOL_DEFAULTS", "ReplayProvider"]


class _SymbolSpec:
    __slots__ = ("base_spread_pips", "daily_vol", "pip", "price")

    def __init__(self, price: float, daily_vol: float, pip: float, base_spread_pips: float):
        self.price = price
        self.daily_vol = daily_vol
        self.pip = pip
        self.base_spread_pips = base_spread_pips


# Roughly realistic mid-2026 levels and annualised vols. Exact values do not
# matter; the *relative* ordering does, so the UI shows a plausible spread of
# volatility across pairs rather than five identical wiggles.
SYMBOL_DEFAULTS: dict[str, _SymbolSpec] = {
    "EURUSD": _SymbolSpec(1.0850, 0.070, 0.0001, 0.6),
    "GBPUSD": _SymbolSpec(1.2720, 0.085, 0.0001, 0.9),
    "USDJPY": _SymbolSpec(147.20, 0.095, 0.01, 0.8),
    "AUDUSD": _SymbolSpec(0.6620, 0.100, 0.0001, 1.1),
    "USDCHF": _SymbolSpec(0.8730, 0.072, 0.0001, 1.0),
    "USDCAD": _SymbolSpec(1.3610, 0.068, 0.0001, 1.2),
    "NZDUSD": _SymbolSpec(0.6040, 0.105, 0.0001, 1.4),
    "EURGBP": _SymbolSpec(0.8530, 0.055, 0.0001, 1.0),
}

_FALLBACK = _SymbolSpec(1.0000, 0.080, 0.0001, 1.0)

# Annualisation basis must match fx_core.models: 252 days x 24h.
_SECONDS_PER_TRADING_YEAR = 252 * 24 * 3600


class ReplayProvider(MarketDataProvider):
    name = "replay"
    supports_backfill = False

    def __init__(
        self,
        symbols: Sequence[str],
        ticks_per_sec: float = 25.0,
        seed: int = 42,
        burst_every_s: float = 90.0,
        burst_duration_s: float = 12.0,
        burst_multiplier: float = 6.0,
        speed: float = 1.0,
    ) -> None:
        super().__init__(symbols)
        self.ticks_per_sec = max(ticks_per_sec, 0.1)
        self.burst_every_s = burst_every_s
        self.burst_duration_s = burst_duration_s
        self.burst_multiplier = burst_multiplier
        self.speed = max(speed, 0.001)  # load tests crank this to 100
        self._rng = random.Random(seed)
        self._specs = {s: SYMBOL_DEFAULTS.get(s, _FALLBACK) for s in self.symbols}
        self._prices = {s: self._specs[s].price for s in self.symbols}
        self._seq = 0
        self._running = False
        self._started_at = 0.0

    async def connect(self) -> None:
        self._running = True
        self._started_at = asyncio.get_running_loop().time()

    async def close(self) -> None:
        self._running = False

    def _burst_factor(self, elapsed: float) -> float:
        """Volatility regime multiplier - gives the Schmitt trigger something real."""
        if self.burst_every_s <= 0:
            return 1.0
        phase = elapsed % self.burst_every_s
        if phase >= self.burst_duration_s:
            return 1.0
        # Smooth ramp up and down, so the alert fires on a genuine regime change
        # rather than on a discontinuity that any threshold would catch.
        x = phase / self.burst_duration_s
        return 1.0 + (self.burst_multiplier - 1.0) * math.sin(math.pi * x)

    def _round_to_pip(self, price: float, pip: float) -> float:
        decimals = max(0, round(-math.log10(pip)) + 1)
        return round(price, decimals)

    async def stream(self) -> AsyncIterator[Tick]:
        loop = asyncio.get_running_loop()
        interval = 1.0 / (self.ticks_per_sec * self.speed)

        while self._running:
            elapsed = (loop.time() - self._started_at) * self.speed
            burst = self._burst_factor(elapsed)
            symbol = self._rng.choice(self.symbols)
            spec = self._specs[symbol]

            # Per-tick sigma from annualised vol: sigma_tick = sigma_ann * sqrt(dt/year)
            dt = 1.0 / self.ticks_per_sec
            sigma = spec.daily_vol * math.sqrt(dt / _SECONDS_PER_TRADING_YEAR) * burst
            self._prices[symbol] *= math.exp(self._rng.gauss(0.0, sigma))
            mid = self._prices[symbol]

            # Spreads widen with volatility. Real market microstructure, one line.
            half = spec.pip * spec.base_spread_pips * burst / 2.0
            self._seq += 1
            now = datetime.now(UTC)

            tick = Tick(
                symbol=symbol,
                bid=self._round_to_pip(mid - half, spec.pip),
                ask=self._round_to_pip(mid + half, spec.pip),
                ts_event=now,
                ts_ingest=now,
                seq=self._seq,
            )
            self.received_frames += 1
            yield tick
            await asyncio.sleep(interval)
