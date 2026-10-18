"""The reconnect ladder, isolated from Redis and from a provider.

Found during the post-audit sweep, in code the audit had measured at 0% coverage.

THE DEFECT
----------
``_run_as_leader`` reset ``attempt`` to 0 immediately after any clean return from
``_consume_once``, then computed the next delay from that reset value:

    await self._consume_once()
    attempt = 0                     # unconditional
    ...
    else:
        delay = self.backoff.delay(attempt)   # always delay(0)
        attempt += 1                          # discarded on the next pass

A session that lasted four hours and a session that was closed by the server in
four milliseconds were treated identically. The escalation therefore never
happened on the clean-disconnect path: every reconnect slept uniform(0, base),
about 250 ms, forever.

That is not a theoretical shape. A provider that accepts the socket and hangs up -
at capacity, load balancer draining, authenticated but not entitled, or simply
closed for the weekend - produces exactly this, and the result is roughly four
reconnects per second against someone else's infrastructure, indefinitely. The
circuit breaker could not save us either, because ``record_success()`` fired on
connect, so the breaker saw an unbroken run of successes.

The module docstring in supervisor.py describes full jitter as making us "a good
citizen of someone else's infrastructure". On this path we were not.
"""

from __future__ import annotations

import asyncio
import random

import pytest
from fx_ingestor.config import IngestorSettings
from fx_ingestor.main import MIN_HEALTHY_SESSION_S, Ingestor
from fx_ingestor.providers import ProviderError
from fx_ingestor.supervisor import BreakerState


class FakeClock:
    """Monotonic time we control, so 'the session lasted an hour' costs no hour."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> tuple[Ingestor, list[float]]:
    """An Ingestor with Redis, the provider and sleeping all replaced."""
    cfg = IngestorSettings(FX_PROVIDER="tiingo", FX_PROVIDER_TOKEN="t", FX_SYMBOLS="EURUSD")
    monkeypatch.setattr("fx_ingestor.main.make_redis", lambda _url: None)
    monkeypatch.setattr("fx_ingestor.supervisor._DEFAULT_RNG", random.Random(0))
    ing = Ingestor(cfg)
    ing._monotonic = clock

    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)
        clock.advance(d)

    monkeypatch.setattr("fx_ingestor.main.asyncio.sleep", fake_sleep)

    async def noop_status(state: str, detail: str = "") -> None:
        return None

    monkeypatch.setattr(ing, "_publish_status", noop_status)
    return ing, slept


async def _drive(ing: Ingestor, session_s: float, rounds: int, clock: FakeClock) -> None:
    """Run N sessions, each lasting `session_s` of simulated time."""
    calls = 0

    async def consume() -> None:
        nonlocal calls
        calls += 1
        clock.advance(session_s)
        if calls >= rounds:
            ing._shutdown.set()

    ing._consume_once = consume  # type: ignore[method-assign]
    await ing._run_as_leader()


async def _drive_drops(ing: Ingestor, session_s: float, rounds: int, clock: FakeClock) -> None:
    """Run N sessions that each end in a dropped socket after `session_s`."""
    calls = 0

    async def consume() -> None:
        nonlocal calls
        calls += 1
        clock.advance(session_s)
        if calls >= rounds:
            ing._shutdown.set()
        raise ProviderError("tiingo connection closed: sent 1011 (internal error)")

    ing._consume_once = consume  # type: ignore[method-assign]
    await ing._run_as_leader()


async def test_a_flapping_provider_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression. Sessions that die instantly must escalate the ladder."""
    clock = FakeClock()
    ing, slept = build(monkeypatch, clock)
    # An open breaker adds 1s waits of its own, which would satisfy the sum below
    # without any escalation. This test is about the ladder alone.
    ing.breaker.fail_threshold = 1_000

    await _drive(ing, session_s=0.01, rounds=6, clock=clock)

    # Full jitter means each delay is uniform in [0, ceiling], so individual
    # values are noisy. The CEILING is what escalates, and the observable
    # consequence is total wait: five delays under the old code could not exceed
    # 5 * base = 2.5s, and the ladder now reaches that in the last step alone.
    assert len(slept) >= 5
    assert sum(slept) > 5 * ing.backoff.base_s, (
        "the clean-disconnect path is not escalating; a provider that hangs up "
        "immediately will be hammered at the base delay forever"
    )


async def test_a_long_session_resets_the_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The behaviour the original code was reaching for, kept intact."""
    clock = FakeClock()
    ing, slept = build(monkeypatch, clock)

    await _drive(ing, session_s=MIN_HEALTHY_SESSION_S * 2, rounds=5, clock=clock)

    # Every session was healthy, so every delay is drawn from the base ceiling.
    assert all(d <= ing.backoff.base_s for d in slept)


async def test_the_breaker_eventually_opens_on_a_connect_hangup_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A socket that opens and closes is a failure, not a success.

    record_success() used to fire the moment the socket opened, so a provider
    stuck in accept-then-hangup kept the breaker CLOSED forever and it never did
    the one job it exists for.
    """
    clock = FakeClock()
    ing, _ = build(monkeypatch, clock)
    ing.breaker.fail_threshold = 3
    # The reset window runs on the real clock, which FakeClock cannot advance;
    # without this each reopen waits 30 real seconds and the test times out.
    ing.breaker.reset_after_s = 0.0

    await _drive(ing, session_s=0.01, rounds=8, clock=clock)

    assert ing.breaker.state is BreakerState.OPEN


async def test_a_healthy_session_closes_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    ing, _ = build(monkeypatch, clock)
    ing.breaker.fail_threshold = 2
    ing.breaker.record_failure()
    ing.breaker.record_failure()
    assert ing.breaker.state is BreakerState.OPEN

    # allows_attempt() gates on the reset window; move past it.
    clock.advance(ing.breaker.reset_after_s + 1)
    await asyncio.sleep(0)
    ing.breaker.reset_after_s = 0.0

    await _drive(ing, session_s=MIN_HEALTHY_SESSION_S * 2, rounds=2, clock=clock)
    assert ing.breaker.state is BreakerState.CLOSED


async def test_drops_after_healthy_sessions_do_not_climb_the_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped socket now ends sessions through the failure path, healthy or not.

    That path never reset the ladder, so on a network that drops the socket every
    hour or so, ten good hours would climb it to the cap and reopen the breaker on
    every drop after the fifth: up to a minute of backoff plus thirty seconds
    of "circuit breaker open" for each one.
    """
    clock = FakeClock()
    ing, slept = build(monkeypatch, clock)

    await _drive_drops(ing, session_s=3_600.0, rounds=10, clock=clock)

    assert len(slept) == 10
    assert all(d <= ing.backoff.base_s for d in slept)
    assert ing.breaker.state is BreakerState.CLOSED
    assert ing.breaker.failures == 1


async def test_drops_that_end_short_sessions_still_escalate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reset is earned by the session, not by the drop."""
    clock = FakeClock()
    ing, slept = build(monkeypatch, clock)
    ing.breaker.fail_threshold = 3
    ing.breaker.reset_after_s = 0.0  # the reset window runs on the real clock

    await _drive_drops(ing, session_s=0.01, rounds=8, clock=clock)

    assert sum(slept) > 5 * ing.backoff.base_s
    assert ing.breaker.state is BreakerState.OPEN
