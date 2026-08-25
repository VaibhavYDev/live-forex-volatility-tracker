"""Composition: seasonality -> baseline -> trigger, for one symbol.

Pure and synchronous. No Redis, no database, no clock of its own - feed it a
sigma and a timestamp, get back an optional ``RegimeTransition``. That is the
entire seam the ingestor will call, and keeping it I/O-free is what lets the
whole detection path be tested in milliseconds against synthetic series.

ORDER OF OPERATIONS (each step exists for a measured reason)
------------------------------------------------------------
    1. gate       - should this bar be evaluated at all?
    2. normalise  - divide out the predictable hour-of-day component
    3. score      - z of ln(sigma_adjusted) against the frozen-or-live baseline
    4. decide     - Schmitt trigger with symmetric, time-based confirmation
    5. learn      - admit the sample to the baseline and profile, but ONLY
                    while NORMAL and armed

Step 5 is last on purpose. Learning from a sample before deciding about it means
each observation partly defines its own reference, which is a subtle way to make
a detector that can never detect anything.

THE GATES, AND THE FALSE POSITIVES THEY EXIST TO KILL
-----------------------------------------------------
**Weekend reopen.** FX closes Friday 21:00 UTC and reopens Sunday 21:00 UTC. A
weekend of news is repriced in the first print. A modest 42-pip repricing gives

    gap |log return| = 0.00388   vs. a typical bar sigma of 1e-4
    z on reopen      = 11.4      -> fires every Sunday, on schedule

Nothing destroys trust in an alerting system faster than a false positive that
arrives on a timetable. Three layers handle it: the gap bar is excluded from
every estimator, alerts are gated for a warm-up period after the session opens,
and the market-closed branch means the weekend contributes nothing at all.

**Backfilled bars.** The same shape of bug bites after any feed outage, not just
weekends - the first bar after a gap spans the whole outage. Milestone 0 already
tags those ``source='backfill'``; this is where that column earns its keep.

**Stale feed.** A frozen price reads as exactly zero volatility, so an
un-gated detector reliably reports "volatility collapsed!" the moment the
connection dies. The single most embarrassing possible false positive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TypedDict

from fx_core.alerts.baseline import BaselineSnapshot, VolatilityBaseline
from fx_core.alerts.hysteresis import (
    Regime,
    RegimeTransition,
    SchmittTrigger,
    TriggerConfig,
    TriggerSnapshot,
)
from fx_core.alerts.seasonality import DiurnalProfile, DiurnalSnapshot
from fx_core.calendar import is_market_open

__all__ = ["DetectorConfig", "DetectorSnapshot", "RegimeDetector"]


class DetectorSnapshot(TypedDict):
    """The whole detection path in one payload, ready for Redis."""

    trigger: TriggerSnapshot
    baseline: BaselineSnapshot
    profile: DiurnalSnapshot


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    # How long after a session open before alerts are armed. Long enough for the
    # reopen to settle, short enough that Sunday evening is not dead to us.
    session_warmup: timedelta = timedelta(minutes=30)
    # Contiguous evaluable bars required after ANY gate before re-arming.
    rearm_bars: int = 3

    def __post_init__(self) -> None:
        if self.session_warmup < timedelta(0):
            raise ValueError("session_warmup must not be negative")
        if self.rearm_bars < 0:
            raise ValueError("rearm_bars must not be negative")


@dataclass(slots=True)
class RegimeDetector:
    """One symbol's full detection path."""

    symbol: str
    config: DetectorConfig = field(default_factory=DetectorConfig)
    trigger_config: TriggerConfig = field(default_factory=TriggerConfig)
    baseline: VolatilityBaseline = field(default_factory=VolatilityBaseline)
    profile: DiurnalProfile = field(default_factory=DiurnalProfile)
    trigger: SchmittTrigger = field(init=False)

    # Injected so the calendar branches are testable without waiting for Sunday.
    # A test suite whose result depends on what day it runs is not a test suite.
    market_is_open: Callable[[datetime], bool] = is_market_open

    _was_open: bool | None = None
    _opened_at: datetime | None = None
    _contiguous_ok: int = 0
    _last_zscore: float | None = None
    _rebaselining: bool = False

    def __post_init__(self) -> None:
        self.trigger = SchmittTrigger(symbol=self.symbol, config=self.trigger_config)

    # ------------------------------------------------------------ inspection
    @property
    def regime(self) -> Regime:
        return self.trigger.regime

    @property
    def armed(self) -> bool:
        return self.trigger.armed

    @property
    def last_zscore(self) -> float | None:
        return self._last_zscore

    # ------------------------------------------------------------------ core
    def update(
        self,
        sigma: float,
        ts: datetime,
        *,
        is_gap_bar: bool = False,
        is_backfill: bool = False,
        feed_is_stale: bool = False,
    ) -> RegimeTransition | None:
        """Feed one sealed bar's volatility. Returns a transition, or None."""
        gate = self._gate(ts, is_gap_bar=is_gap_bar, is_backfill=is_backfill, stale=feed_is_stale)
        if gate is not None:
            self._contiguous_ok = 0
            self._last_zscore = None
            return self.trigger.update(None, sigma, ts, armed=False, gate_reason=gate)

        # Re-arming deliberately costs a few bars. The bar immediately after a
        # gate is the one most likely to carry the artefact the gate existed to
        # exclude, so trusting it straight away would defeat the gate.
        self._contiguous_ok += 1
        if self._contiguous_ok <= self.config.rearm_bars:
            self._last_zscore = None
            return self.trigger.update(
                None, sigma, ts, armed=False, gate_reason="re-arming after gate"
            )

        adjusted = self.profile.deseasonalize(sigma, ts.hour)
        zscore = self.baseline.zscore(adjusted)
        self._last_zscore = zscore

        # Bounded freeze: hold the pre-event reference during stress, but let it
        # re-learn if the stress turns out to be the new normal. See
        # SchmittTrigger.baseline_should_thaw for the reasoning and the cost.
        #
        # `_rebaselining` is load-bearing. Thawing alone does nothing: the freeze
        # below would immediately re-apply, and admission is otherwise gated on
        # being NORMAL - so the baseline would never actually see the elevated
        # level and z would never fall. Re-baselining has to suspend BOTH rules
        # until the regime resolves.
        if self.trigger.baseline_should_thaw(ts) and not self._rebaselining:
            self._rebaselining = True
            self.baseline.thaw()
            self.trigger.mark_thawed()

        transition = self.trigger.update(zscore, adjusted, ts)
        if transition is not None:
            self._rebaselining = False

        if self.trigger.regime is Regime.STRESSED and not self._rebaselining:
            self.baseline.freeze()
        elif self.trigger.regime is Regime.NORMAL:
            self.baseline.thaw()

        # Learn only from normal conditions (module docstring, step 5) - or while
        # deliberately re-baselining an accepted regime.
        if self.trigger.regime is Regime.NORMAL or self._rebaselining:
            self.baseline.observe(adjusted)
        if self.trigger.regime is Regime.NORMAL:
            # The seasonal profile describes what a typical Tuesday looks like.
            # Learning it during a crisis bakes the crisis into "typical".
            self.profile.observe(sigma, ts.hour)

        return transition

    # ----------------------------------------------------------------- gates
    def _gate(
        self, ts: datetime, *, is_gap_bar: bool, is_backfill: bool, stale: bool
    ) -> str | None:
        """Returns a reason string to suppress evaluation, or None to proceed."""
        open_now = self.market_is_open(ts)

        if self._was_open is False and open_now:
            self._opened_at = ts
        self._was_open = open_now

        if not open_now:
            return "market closed"
        if self._opened_at is not None and (ts - self._opened_at) < self.config.session_warmup:
            return "session warm-up"
        if is_gap_bar:
            return "session gap bar"
        if is_backfill:
            return "backfilled bar"
        if stale:
            return "feed stale"
        return None

    # ----------------------------------------------------------- rehydration
    def snapshot(self) -> DetectorSnapshot:
        """Everything a promoted standby needs to avoid restarting cold.

        Without this a failover loses the event: the standby re-learns its
        baseline from the elevated data, decides the crisis is normal, and never
        emits the clear. Verified against a real Redis in
        tests/chaos/test_regime_pipeline.py.
        """
        return DetectorSnapshot(
            trigger=self.trigger.snapshot(),
            baseline=self.baseline.snapshot(),
            profile=self.profile.snapshot(),
        )

    def restore(self, state: DetectorSnapshot) -> None:
        self.trigger = SchmittTrigger.restore(self.symbol, state["trigger"], self.trigger_config)
        self.baseline = VolatilityBaseline.restore(
            state["baseline"], min_samples=self.baseline.min_samples
        )
        self.profile = DiurnalProfile.restore(
            state["profile"],
            min_samples=self.profile.min_samples,
            min_ready_hours=self.profile.min_ready_hours,
        )
