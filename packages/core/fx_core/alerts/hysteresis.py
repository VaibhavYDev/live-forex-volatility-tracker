"""Volatility regime detection: a Schmitt trigger over a z-score.

This module is ONLY the state machine. The statistics it consumes live in
``baseline.py`` (log-space EWMA/EWMV, freezable) and ``seasonality.py``
(time-of-day normalisation); ``detector.py`` composes all three. That split is
deliberate: the state machine is about *committing to a decision under noise*,
which is a different problem from *measuring how unusual a number is*, and
tangling them makes both untestable.

WHAT A SCHMITT TRIGGER IS FOR
-----------------------------
A single threshold produces alert storms: a signal hovering at the boundary
crosses it hundreds of times a minute. Two thresholds - fire high, clear low -
mean that once triggered, the signal must fall *substantially* before the alert
clears. Borrowed from analogue electronics, where the problem was solved in the
1930s and the answer has not needed improving since.

    z >= enter_z  ->  NORMAL becomes STRESSED
    z <= exit_z   ->  STRESSED becomes NORMAL
    in between    ->  whatever we already were

THREE THINGS THIS VERSION FIXES
-------------------------------
1. **Symmetric confirmation.** The previous implementation required three
   consecutive samples to fire but cleared on ONE sample below ``exit_z``. A
   single noisy low print mid-crisis silently disarmed the alert. Noise
   protection that only guards one direction is not noise protection.

2. **Confirmation measured in wall-clock, not samples.** Updates arrive when a
   bar seals, and a bar seals only when a tick arrives in the *next* minute. In
   the Asian lull "three consecutive updates" can span half an hour. The
   confirmation window now requires BOTH a sample count and an elapsed duration,
   so its meaning is stable whatever the tick rate. A gap longer than
   ``sample_timeout`` resets the candidate, because samples straddling an outage
   are not consecutive observations of anything.

3. **Regime is separated from bookkeeping.** The old ``AlertState`` mixed the
   thing you publish (NORMAL / STRESSED) with internal candidate counting and
   rate limiting. ``Regime`` is now exactly the committed, persistable state, so
   ``old_regime -> new_regime`` falls straight out of the machine.

THE ALTERNATION INVARIANT
-------------------------
Consecutive transitions for a symbol MUST alternate NORMAL -> STRESSED ->
NORMAL. A hole means an event was dropped somewhere in the pipeline. It is
enforced here, asserted in the tests, and worth exposing as a metric once wired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TypedDict

__all__ = [
    "Regime",
    "RegimeTransition",
    "SchmittTrigger",
    "TransitionCause",
    "TriggerConfig",
    "TriggerSnapshot",
]


class TriggerSnapshot(TypedDict):
    """Persisted shape, so a promoted standby does not restart cold.

    Candidate progress is deliberately absent: a half-finished confirmation is
    evidence held by a process that no longer exists, and re-earning three
    samples costs seconds.
    """

    regime: str
    seq: int
    regime_since: str | None
    last_escalation: str | None
    thawed: bool


class Regime(StrEnum):
    """The committed state. This is what gets published and persisted."""

    NORMAL = "normal"
    STRESSED = "stressed"

    def flip(self) -> Regime:
        return Regime.STRESSED if self is Regime.NORMAL else Regime.NORMAL


class TransitionCause(StrEnum):
    """Why the regime changed.

    Persisted alongside the transition because these mean very different things
    in a postmortem, and a bare "STRESSED -> NORMAL" cannot distinguish them:

    * ``THRESHOLD``       - the z-score genuinely crossed. The market moved.
    * ``BASELINE_THAW``   - elevated volatility persisted so long that we
                            re-baselined and accepted it as the new normal. The
                            market did NOT calm down; our yardstick moved.
    * ``OBSERVATION_LOST``- we stopped being able to measure. Not a claim about
                            the market at all.
    """

    THRESHOLD = "threshold"
    BASELINE_THAW = "baseline_thaw"
    OBSERVATION_LOST = "observation_lost"


@dataclass(frozen=True, slots=True)
class RegimeTransition:
    """One committed regime change.

    ``seq`` is a per-symbol monotonic counter, and it is the idempotency key:
    the persister can enforce ``UNIQUE (symbol, seq)`` so an at-least-once
    redelivery collapses to the same row. A stream id would not work - the same
    logical transition can be re-emitted under a new id after a failover.
    """

    symbol: str
    ts: datetime
    seq: int
    old_regime: Regime
    new_regime: Regime
    trigger_value: float  # the z-score that caused the commit
    threshold_value: float  # the threshold it crossed
    sigma: float  # deseasonalised sigma at commit time
    cause: TransitionCause = TransitionCause.THRESHOLD
    reason: str = ""

    @property
    def is_escalation(self) -> bool:
        return self.new_regime is Regime.STRESSED


@dataclass(frozen=True, slots=True)
class TriggerConfig:
    enter_z: float = 3.0
    exit_z: float = 1.5  # MUST be < enter_z; that gap IS the hysteresis band

    # Confirmation requires BOTH, in either direction. See point 2 above.
    min_confirm_samples: int = 3
    confirm_for: timedelta = timedelta(minutes=3)

    # A gap longer than this means the next sample is not "consecutive" with the
    # last one, so the candidate is abandoned rather than completed across a hole.
    sample_timeout: timedelta = timedelta(minutes=15)

    # Rate limit on ESCALATION only. Delaying a clear would leave a stale alert
    # on screen, which is a worse failure than an extra escalation.
    cooldown: timedelta = timedelta(minutes=5)

    # How long elevated volatility may persist before we accept it as the new
    # normal and let the baseline re-learn. See `baseline_should_thaw`.
    thaw_after: timedelta = timedelta(hours=6)

    # If we cannot evaluate for this long while STRESSED, stop asserting a
    # regime we are no longer observing.
    observation_timeout: timedelta = timedelta(minutes=30)

    def __post_init__(self) -> None:
        if self.exit_z >= self.enter_z:
            raise ValueError(
                f"exit_z ({self.exit_z}) must be below enter_z ({self.enter_z}); "
                "equal thresholds reintroduce the alert storm this class exists to prevent"
            )
        if self.min_confirm_samples < 1:
            raise ValueError("min_confirm_samples must be at least 1")
        if self.confirm_for < timedelta(0):
            raise ValueError("confirm_for must not be negative")
        if self.sample_timeout <= timedelta(0):
            raise ValueError("sample_timeout must be positive")
        if self.sample_timeout <= self.confirm_for:
            # Otherwise every candidate is abandoned by the staleness rule before
            # its confirmation window can close, and the trigger never fires at all.
            raise ValueError(
                f"sample_timeout ({self.sample_timeout}) must exceed confirm_for "
                f"({self.confirm_for}), or no candidate can ever complete"
            )


@dataclass(slots=True)
class SchmittTrigger:
    """Regime state machine for one symbol.

    Pure and synchronous: feed it a z-score and a timestamp, get back a
    ``RegimeTransition`` on the updates where the committed regime changed, and
    ``None`` on every other update. It owns no clock, no I/O and no statistics.
    """

    symbol: str
    config: TriggerConfig = field(default_factory=TriggerConfig)
    regime: Regime = Regime.NORMAL
    seq: int = 0

    _regime_since: datetime | None = None
    _candidate: Regime | None = None
    _candidate_since: datetime | None = None
    _candidate_samples: int = 0
    _last_sample_ts: datetime | None = None
    _last_escalation: datetime | None = None
    _unobserved_since: datetime | None = None
    _thawed: bool = False
    _gate_reason: str = ""

    # ------------------------------------------------------------- inspection
    @property
    def armed(self) -> bool:
        """True when the last update was actually evaluated."""
        return self._unobserved_since is None

    @property
    def gate_reason(self) -> str:
        """Why the last update was not evaluated. Empty when armed."""
        return self._gate_reason

    @property
    def candidate(self) -> Regime | None:
        """The regime we are currently accumulating confirmation for."""
        return self._candidate

    @property
    def stressed_since(self) -> datetime | None:
        return self._regime_since if self.regime is Regime.STRESSED else None

    def baseline_should_thaw(self, now: datetime) -> bool:
        """Has this stress event lasted long enough to be the new normal?

        Freezing the baseline during stress is what stops volatility clustering
        from stretching the yardstick until the alert clears itself (measured:
        a rolling baseline drops below exit_z after ~20 bars of a regime that
        never ends). But a frozen baseline held forever is its own bug: if a peg
        breaks or a central bank changes policy, "normal" genuinely moved and we
        would sit STRESSED against a reference that no longer exists.

        So the freeze is bounded. After ``thaw_after`` the caller thaws the
        baseline, it re-learns the elevated level, z falls on its own, and the
        ordinary clear fires - tagged ``BASELINE_THAW`` so the record says
        "we re-baselined", not "the market calmed down". Those are different
        claims and only one of them is true.
        """
        if self.regime is not Regime.STRESSED or self._regime_since is None:
            return False
        return (now - self._regime_since) >= self.config.thaw_after

    def mark_thawed(self) -> None:
        """Told by the caller that the baseline was thawed for this event.

        Tags the next return to NORMAL as ``BASELINE_THAW`` rather than
        ``THRESHOLD``.
        """
        self._thawed = True

    # ------------------------------------------------------------------ core
    def update(
        self,
        zscore: float | None,
        sigma: float,
        ts: datetime,
        *,
        armed: bool = True,
        gate_reason: str = "",
    ) -> RegimeTransition | None:
        """Feed one observation. Returns a transition only on a committed change.

        ``zscore is None`` means the baseline is not yet trustworthy;
        ``armed=False`` means an external gate says do not evaluate (market
        closed, session warm-up, backfilled bar, stale feed). Both hold the
        current regime rather than asserting a new one - see ``_hold``.
        """
        if not armed or zscore is None:
            return self._hold(ts, gate_reason or ("baseline not ready" if armed else "gated"))

        self._unobserved_since = None
        self._gate_reason = ""

        # A gap longer than sample_timeout breaks the chain of consecutive
        # observations. Completing a confirmation across an outage would mean
        # committing on evidence that is partly from before it.
        if (
            self._last_sample_ts is not None
            and (ts - self._last_sample_ts) > self.config.sample_timeout
        ):
            self._reset_candidate()
        self._last_sample_ts = ts

        target = self._target_regime(zscore)
        if target is self.regime:
            self._reset_candidate()
            return None

        return self._accumulate(target, zscore, sigma, ts)

    def _target_regime(self, zscore: float) -> Regime:
        """The Schmitt trigger itself.

        Note the asymmetry: which threshold applies depends on where we already
        are. Inside the band [exit_z, enter_z) neither fires, so the current
        regime persists. That band is the entire mechanism.
        """
        if self.regime is Regime.NORMAL:
            return Regime.STRESSED if zscore >= self.config.enter_z else Regime.NORMAL
        return Regime.NORMAL if zscore <= self.config.exit_z else Regime.STRESSED

    def _accumulate(
        self, target: Regime, zscore: float, sigma: float, ts: datetime
    ) -> RegimeTransition | None:
        if self._candidate is not target:
            self._candidate = target
            self._candidate_since = ts
            self._candidate_samples = 1
        else:
            self._candidate_samples += 1

        assert self._candidate_since is not None
        held_for = ts - self._candidate_since
        if (
            self._candidate_samples < self.config.min_confirm_samples
            or held_for < self.config.confirm_for
        ):
            return None

        # Rate-limit escalations only. A clear is never delayed: leaving a stale
        # STRESSED banner up is worse than an extra escalation in the log.
        if (
            target is Regime.STRESSED
            and self._last_escalation is not None
            and (ts - self._last_escalation) < self.config.cooldown
        ):
            return None

        threshold = self.config.enter_z if target is Regime.STRESSED else self.config.exit_z
        cause = TransitionCause.THRESHOLD
        reason = (
            f"z={zscore:.2f} {'>=' if target is Regime.STRESSED else '<='} {threshold} "
            f"held {held_for.total_seconds():.0f}s over {self._candidate_samples} samples"
        )
        if target is Regime.NORMAL and self._thawed:
            cause = TransitionCause.BASELINE_THAW
            reason = f"baseline thawed; elevated level accepted as normal ({reason})"

        return self._commit(target, zscore, threshold, sigma, ts, cause, reason)

    def _hold(self, ts: datetime, reason: str) -> RegimeTransition | None:
        """Cannot evaluate: hold the regime, abandon any candidate.

        Deliberately NOT forcing NORMAL. A dead feed reads as zero volatility,
        and "we stopped observing" is not evidence that the market calmed - the
        old implementation's `suppressed` event asserted exactly that. The feed
        status channel already tells the UI the data is degraded; the regime
        should keep saying what it last actually measured.

        But not forever. After ``observation_timeout`` we stop asserting a
        regime we have no evidence for, and say so with a distinct cause.
        """
        self._gate_reason = reason
        self._reset_candidate()

        if self._unobserved_since is None:
            self._unobserved_since = ts

        if (
            self.regime is Regime.STRESSED
            and (ts - self._unobserved_since) >= self.config.observation_timeout
        ):
            return self._commit(
                Regime.NORMAL,
                float("nan"),
                self.config.exit_z,
                float("nan"),
                ts,
                TransitionCause.OBSERVATION_LOST,
                f"no evaluable samples for "
                f"{(ts - self._unobserved_since).total_seconds():.0f}s ({reason})",
            )
        return None

    def _commit(
        self,
        target: Regime,
        zscore: float,
        threshold: float,
        sigma: float,
        ts: datetime,
        cause: TransitionCause,
        reason: str,
    ) -> RegimeTransition:
        if target is self.regime:  # pragma: no cover - guarded by every caller
            raise AssertionError(
                f"alternation invariant violated: committing {target} while already {self.regime}"
            )

        old = self.regime
        self.regime = target
        self.seq += 1
        self._regime_since = ts
        self._reset_candidate()
        self._thawed = False
        if target is Regime.STRESSED:
            self._last_escalation = ts

        return RegimeTransition(
            symbol=self.symbol,
            ts=ts,
            seq=self.seq,
            old_regime=old,
            new_regime=target,
            trigger_value=zscore,
            threshold_value=threshold,
            sigma=sigma,
            cause=cause,
            reason=reason,
        )

    def _reset_candidate(self) -> None:
        self._candidate = None
        self._candidate_since = None
        self._candidate_samples = 0

    # ---------------------------------------------------------- rehydration
    def snapshot(self) -> TriggerSnapshot:
        """Serialisable state, for surviving a leader failover.

        A new leader that starts cold in NORMAL loses the event entirely: it
        re-baselines on the elevated data, reports NORMAL, and never emits the
        clear. Pure dict in, pure dict out; the caller decides where to put it.

        Candidate progress is deliberately NOT carried across: a half-finished
        confirmation is evidence held by a process that no longer exists, and
        re-earning three samples costs seconds.
        """
        return TriggerSnapshot(
            regime=str(self.regime),
            seq=self.seq,
            regime_since=self._regime_since.isoformat() if self._regime_since else None,
            last_escalation=(self._last_escalation.isoformat() if self._last_escalation else None),
            thawed=self._thawed,
        )

    @classmethod
    def restore(
        cls, symbol: str, state: TriggerSnapshot, config: TriggerConfig | None = None
    ) -> SchmittTrigger:
        def _dt(raw: str | None) -> datetime | None:
            return datetime.fromisoformat(raw) if raw else None

        trigger = cls(symbol=symbol, config=config or TriggerConfig())
        trigger.regime = Regime(state["regime"])
        trigger.seq = state["seq"]
        trigger._regime_since = _dt(state["regime_since"])
        trigger._last_escalation = _dt(state["last_escalation"])
        trigger._thawed = state["thawed"]
        return trigger
