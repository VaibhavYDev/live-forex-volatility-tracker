"""Connection supervision: backoff, staleness watchdog, circuit breaker.

Three failure modes, three mechanisms. Each is a few lines; together they are the
difference between a demo and something you would leave running.


1. FULL JITTER BACKOFF
----------------------
    sleep = random_between(0, min(cap, base * 2^attempt))

Plain exponential backoff synchronises every client on Earth to reconnect at the
same instant when a provider recovers - a thundering herd that immediately re-downs
the provider you were waiting for. Full jitter (AWS Architecture Blog, "Exponential
Backoff and Jitter") spreads them uniformly. It is one line of code and it makes
you a good citizen of someone else's infrastructure.


2. STALENESS WATCHDOG
---------------------
The subtle one. **A dead socket and a quiet market are byte-for-byte identical** -
both produce silence. TCP will happily hold a black-holed connection open for
minutes without an error. So protocol-level pings are not enough; we need an
application-level "no *data* in N seconds -> assume dead, force reconnect".

But naively that watchdog reconnect-loops all weekend, because FX closes Friday
17:00 New York and reopens Sunday 17:00 New York. So it consults
``fx_core.calendar`` and stands down when the market is legitimately closed.


3. CIRCUIT BREAKER
------------------
CLOSED -> OPEN after N consecutive failures -> HALF_OPEN probes -> CLOSED.

While OPEN we stop hammering the provider *and* publish a ``degraded`` status so
the UI can show "Data delayed - reconnecting" with the last-good timestamp.
Silently rendering stale prices as if they were live is the one thing a market
dashboard must never do.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

import structlog
from fx_core.calendar import is_market_open, seconds_until_open

log = structlog.get_logger(__name__)

# Module-level generator so the default is a real Random, not the `random` module.
# Tests inject a seeded one to make backoff deterministic.
_DEFAULT_RNG = random.Random()

__all__ = ["BackoffPolicy", "BreakerState", "CircuitBreaker", "StalenessWatchdog"]


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    base_s: float = 0.5
    cap_s: float = 60.0

    def delay(self, attempt: int, rng: random.Random | None = None) -> float:
        """Full jitter: uniform in [0, min(cap, base * 2^attempt)]."""
        rng = rng or _DEFAULT_RNG
        ceiling = min(self.cap_s, self.base_s * (2.0**attempt))
        return rng.uniform(0.0, ceiling)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class CircuitBreaker:
    fail_threshold: int = 5
    reset_after_s: float = 30.0
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    _opened_at: float = 0.0

    def record_success(self) -> None:
        if self.state is not BreakerState.CLOSED:
            log.info("breaker.closed", after_failures=self.failures)
        self.state = BreakerState.CLOSED
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.fail_threshold and self.state is not BreakerState.OPEN:
            self.state = BreakerState.OPEN
            self._opened_at = time.monotonic()
            log.error("breaker.opened", failures=self.failures, reset_after_s=self.reset_after_s)

    def allows_attempt(self) -> bool:
        if self.state is BreakerState.CLOSED:
            return True
        if self.state is BreakerState.OPEN:
            if time.monotonic() - self._opened_at >= self.reset_after_s:
                self.state = BreakerState.HALF_OPEN
                log.info("breaker.half_open")
                return True
            return False
        return True  # HALF_OPEN: allow exactly one probe

    @property
    def is_degraded(self) -> bool:
        return self.state is not BreakerState.CLOSED


class StalenessWatchdog:
    """Fires when no data has arrived for ``timeout_s`` *while the market is open*."""

    def __init__(
        self,
        timeout_s: float,
        on_stale: Callable[[], Awaitable[None]],
        check_interval_s: float = 1.0,
        market_is_open: Callable[[], bool] = is_market_open,
    ) -> None:
        self._timeout_s = timeout_s
        self._on_stale = on_stale
        self._check_interval_s = check_interval_s
        # Injected so tests can exercise both branches deterministically. A test
        # suite whose result depends on what day it is run is not a test suite -
        # this one would silently stop testing anything every weekend.
        self._market_is_open = market_is_open
        self._last_data = time.monotonic()
        self._task: asyncio.Task[None] | None = None
        self.trips = 0

    def pet(self) -> None:
        """Call on every inbound frame - including heartbeats.

        Heartbeats count deliberately: they prove the socket is alive even when
        the market has nothing to say, which is exactly the case we must not
        mistake for a dead connection.
        """
        self._last_data = time.monotonic()

    @property
    def idle_s(self) -> float:
        return time.monotonic() - self._last_data

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval_s)

            if not self._market_is_open():
                # Market is legitimately closed. Silence is expected, not a fault.
                # Without this branch the watchdog would reconnect-loop from Friday
                # evening to Sunday evening and quite possibly get the key throttled.
                self.pet()
                sleep_s = min(seconds_until_open(), 300.0)
                log.debug("watchdog.market_closed", sleeping_s=round(sleep_s))
                await asyncio.sleep(sleep_s)
                continue

            if self.idle_s > self._timeout_s:
                self.trips += 1
                log.warning("watchdog.stale", idle_s=round(self.idle_s, 1), trips=self.trips)
                self.pet()  # reset before the callback so we do not fire in a loop
                await self._on_stale()

    async def __aenter__(self) -> StalenessWatchdog:
        self.pet()
        self._task = asyncio.create_task(self._loop(), name="staleness-watchdog")
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
