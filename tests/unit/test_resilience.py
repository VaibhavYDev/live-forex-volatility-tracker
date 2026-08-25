"""Backoff, circuit breaker, session calendar, Redis key invariants.

Regime detection and hysteresis moved to test_regime_detection.py when the
alerting model grew from "an alert fired" to "the regime transitioned".
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest
from fx_core import keys
from fx_core.calendar import (
    active_sessions,
    is_market_open,
    next_close,
    next_open,
    seconds_until_open,
)
from fx_ingestor.supervisor import BackoffPolicy, BreakerState, CircuitBreaker


class TestBackoff:
    def test_never_exceeds_the_cap(self) -> None:
        policy = BackoffPolicy(base_s=0.5, cap_s=10.0)
        rng = random.Random(0)
        assert all(0.0 <= policy.delay(a, rng) <= 10.0 for a in range(40))

    def test_full_jitter_spreads_clients_out(self) -> None:
        """The point of jitter: no two clients reconnect at the same instant.

        Plain exponential backoff would return the SAME value every time for a
        given attempt, synchronising every client on the planet into a thundering
        herd the moment the provider recovers.
        """
        policy = BackoffPolicy(base_s=1.0, cap_s=60.0)
        rng = random.Random(42)
        delays = [policy.delay(5, rng) for _ in range(200)]
        assert len(set(delays)) > 190  # essentially all distinct
        # Uniform on [0, 32] -> mean near 16, and small values genuinely occur.
        assert 12.0 < sum(delays) / len(delays) < 20.0
        assert min(delays) < 2.0

    def test_ceiling_grows_exponentially_then_flattens(self) -> None:
        policy = BackoffPolicy(base_s=1.0, cap_s=8.0)
        rng = random.Random(1)
        assert max(policy.delay(0, rng) for _ in range(500)) <= 1.0
        assert max(policy.delay(2, rng) for _ in range(500)) <= 4.0
        assert max(policy.delay(20, rng) for _ in range(500)) <= 8.0


class TestCircuitBreaker:
    def test_opens_after_threshold_and_blocks_attempts(self) -> None:
        cb = CircuitBreaker(fail_threshold=3, reset_after_s=60.0)
        for _ in range(2):
            cb.record_failure()
        assert cb.state is BreakerState.CLOSED
        assert cb.allows_attempt()

        cb.record_failure()
        assert cb.state is BreakerState.OPEN
        assert not cb.allows_attempt()
        assert cb.is_degraded

    def test_half_opens_after_reset_then_closes_on_success(self) -> None:
        cb = CircuitBreaker(fail_threshold=1, reset_after_s=0.0)
        cb.record_failure()
        assert cb.state is BreakerState.OPEN
        assert cb.allows_attempt()  # reset window elapsed -> probe allowed
        assert cb.state is BreakerState.HALF_OPEN
        cb.record_success()
        assert cb.state is BreakerState.CLOSED
        assert cb.failures == 0


class TestFxSessionCalendar:
    """August 2026 is EDT, so 17:00 New York == 21:00 UTC."""

    @pytest.mark.parametrize(
        ("ts", "expected"),
        [
            (datetime(2026, 8, 19, 3, 0, tzinfo=UTC), True),  # Wednesday
            (datetime(2026, 8, 21, 20, 59, tzinfo=UTC), True),  # Fri, just before close
            (datetime(2026, 8, 21, 21, 1, tzinfo=UTC), False),  # Fri, just after close
            (datetime(2026, 8, 22, 12, 0, tzinfo=UTC), False),  # Saturday
            (datetime(2026, 8, 23, 20, 59, tzinfo=UTC), False),  # Sun, before open
            (datetime(2026, 8, 23, 21, 1, tzinfo=UTC), True),  # Sun, after open
        ],
    )
    def test_week_boundaries(self, ts: datetime, expected: bool) -> None:
        assert is_market_open(ts) is expected

    def test_dst_shifts_the_utc_boundary(self) -> None:
        """January is EST (UTC-5), so the close is 22:00 UTC, not 21:00.

        Hardcoding either value would be wrong for half the year - which is why
        the calendar anchors in America/New_York rather than in UTC.
        """
        assert is_market_open(datetime(2026, 1, 16, 21, 30, tzinfo=UTC)) is True
        assert is_market_open(datetime(2026, 1, 16, 22, 30, tzinfo=UTC)) is False
        # Same clock time in August is already closed.
        assert is_market_open(datetime(2026, 8, 21, 21, 30, tzinfo=UTC)) is False

    def test_weekend_gap_is_about_two_days(self) -> None:
        saturday = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
        hours = seconds_until_open(saturday) / 3600
        assert 32 < hours < 34
        assert next_open(saturday) == datetime(2026, 8, 23, 21, 0, tzinfo=UTC)

    def test_no_wait_when_already_open(self) -> None:
        assert seconds_until_open(datetime(2026, 8, 19, 3, 0, tzinfo=UTC)) == 0.0

    def test_next_close_is_the_coming_friday(self) -> None:
        assert next_close(datetime(2026, 8, 19, 3, 0, tzinfo=UTC)) == datetime(
            2026, 8, 21, 21, 0, tzinfo=UTC
        )

    def test_sessions_overlap(self) -> None:
        """London/New York overlap is the highest-liquidity window of the day."""
        overlap = active_sessions(datetime(2026, 8, 19, 14, 0, tzinfo=UTC))
        assert "London" in overlap
        assert "New York" in overlap

    def test_no_sessions_when_closed(self) -> None:
        assert active_sessions(datetime(2026, 8, 22, 12, 0, tzinfo=UTC)) == ()


class TestRedisKeyInvariant:
    def test_durable_keys_are_never_given_a_ttl(self) -> None:
        """The invariant that makes `maxmemory-policy volatile-lru` safe.

        TTL = disposable, no TTL = durable. Under volatile-lru Redis only evicts
        keys that carry a TTL, so the WAL and the leader lease are structurally
        immune to eviction. If someone ever adds a TTL to one of these, this test
        is the tripwire.
        """
        assert keys.STREAM_TICKS in keys.DURABLE_KEYS
        assert keys.LEADER_LEASE in keys.DURABLE_KEYS

        disposable = {
            keys.quote("EURUSD"),
            keys.bar_bucket("EURUSD", 1_755_859_200),
            keys.vol_state("EURUSD", "1h"),
            keys.history("EURUSD"),
        }
        assert not (disposable & keys.DURABLE_KEYS)

        for ttl in (keys.TTL_QUOTE_S, keys.TTL_BAR_S, keys.TTL_VOL_S, keys.TTL_HISTORY_S):
            assert ttl > 0

    def test_namespaces_do_not_collide(self) -> None:
        generated = [
            keys.quote("EURUSD"),
            keys.bar_bucket("EURUSD", 1),
            keys.vol_state("EURUSD", "1h"),
            keys.history("EURUSD"),
            keys.channel_tick("EURUSD"),
            keys.channel_vol("EURUSD"),
            keys.STREAM_TICKS,
            keys.LEADER_LEASE,
        ]
        assert len(set(generated)) == len(generated)
