"""Backpressure by conflation - the behaviour that keeps the server alive.

These tests exist because "we handle slow clients" is a claim, and a claim
without a test is a wish.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest
import redis
from fx_api.ws.conflator import ClientSession
from fx_api.ws.manager import ConnectionManager
from fx_core import keys


def _session() -> ClientSession:
    s = ClientSession(client_id="c1")
    s.subscribe(["EURUSD", "GBPUSD"])
    return s


class TestConflation:
    def test_slow_client_gets_only_the_newest_price(self) -> None:
        """The core property. A stale price is worthless, so drop it."""
        s = _session()
        for i in range(1000):
            s.offer("tick:EURUSD", {"m": 1.0800 + i * 1e-5})

        batch = s.drain()
        assert len(batch) == 1
        assert batch[0]["m"] == 1.0800 + 999e-5
        assert s.conflated == 999

    def test_memory_is_bounded_by_symbol_count_not_message_rate(self) -> None:
        """The OOM this design prevents.

        A naive unbounded queue would hold 50_000 entries here. The conflating
        map holds one per subscribed symbol, whatever the rate.
        """
        s = _session()
        for i in range(50_000):
            s.offer(f"tick:{'EURUSD' if i % 2 else 'GBPUSD'}", {"i": i})
        assert len(s.pending) == 2

    def test_distinct_kinds_do_not_overwrite_each_other(self) -> None:
        """A volatility update must not silently replace a price update."""
        s = _session()
        s.offer("tick:EURUSD", {"kind": "tick"})
        s.offer("vol:EURUSD", {"kind": "vol"})
        kinds = {f["kind"] for f in s.drain()}
        assert kinds == {"tick", "vol"}

    def test_fast_client_loses_nothing(self) -> None:
        """Conflation must be invisible to a client that keeps up."""
        s = _session()
        received = []
        for i in range(100):
            s.offer("tick:EURUSD", {"i": i})
            received.extend(s.drain())
        assert [r["i"] for r in received] == list(range(100))
        assert s.conflated == 0

    def test_offer_is_ignored_after_close(self) -> None:
        s = _session()
        s.closed = True
        s.offer("tick:EURUSD", {"m": 1.08})
        assert not s.pending


class TestSubscriptions:
    def test_filtering_is_server_side(self) -> None:
        s = ClientSession(client_id="c1")
        s.subscribe(["EURUSD"])
        assert s.wants("EURUSD")
        assert s.wants("eurusd")  # case-insensitive
        assert not s.wants("USDJPY")

    def test_unsubscribe_drops_queued_updates_immediately(self) -> None:
        """Otherwise an unsubscribe only takes effect after the next flush."""
        s = _session()
        s.offer("tick:EURUSD", {"m": 1.08})
        s.offer("tick:GBPUSD", {"m": 1.27})
        s.unsubscribe(["EURUSD"])
        assert "tick:EURUSD" not in s.pending
        assert "tick:GBPUSD" in s.pending
        assert not s.wants("EURUSD")


class TestSaturationDetection:
    def test_empty_queue_is_never_saturated(self) -> None:
        s = _session()
        assert not s.note_saturation(deadline_s=0.0)

    def test_occasional_conflation_is_not_saturation(self) -> None:
        """Conflating is the mechanism WORKING; only a persistent backlog is bad."""
        s = _session()
        s.offer("tick:EURUSD", {"m": 1.08})
        assert not s.note_saturation(deadline_s=5.0)  # first observation only arms it
        s.drain()
        assert not s.note_saturation(deadline_s=5.0)

    def test_persistent_backlog_trips_the_deadline(self) -> None:
        s = _session()
        s.offer("tick:EURUSD", {"m": 1.08})
        assert not s.note_saturation(deadline_s=0.05)
        time.sleep(0.06)
        assert s.note_saturation(deadline_s=0.05)

    def test_draining_resets_the_saturation_clock(self) -> None:
        s = _session()
        s.offer("tick:EURUSD", {"m": 1.08})
        s.note_saturation(deadline_s=0.05)
        time.sleep(0.06)
        s.drain()
        assert not s.note_saturation(deadline_s=0.05)

    def test_a_reader_keeping_up_with_a_busy_feed_is_never_saturated(self) -> None:
        """The defect this pins: every reader of a busy market was dropped.

        The writer wakes because an update arrived, so on a busy feed the queue
        is never empty when it looks. Timing how long the queue LOOKED non-empty
        disconnected a client that took every update at once, ten seconds in.
        """
        s = _session()
        end = time.monotonic() + 0.25  # five deadlines
        while time.monotonic() < end:
            s.offer("tick:EURUSD", {"m": 1.08})  # an update arrives; the writer wakes
            assert not s.note_saturation(deadline_s=0.05)
            s.drain()  # and takes it at once
            time.sleep(0.005)

    def test_the_clock_is_the_age_of_the_oldest_queued_update(self) -> None:
        s = _session()
        s.offer("tick:EURUSD", {"m": 1.08})
        time.sleep(0.04)
        s.offer("tick:GBPUSD", {"m": 1.27})  # newer data does not restart the clock
        assert not s.note_saturation(deadline_s=0.05)
        time.sleep(0.03)
        assert s.note_saturation(deadline_s=0.05)

    def test_an_unsubscribe_that_empties_the_queue_is_not_a_backlog(self) -> None:
        s = _session()
        s.offer("tick:EURUSD", {"m": 1.08})
        time.sleep(0.06)
        s.unsubscribe(["EURUSD"])
        assert not s.note_saturation(deadline_s=0.05)


class _FlakyPubSub:
    """listen() fails once, as it does when Redis outlasts redis-py's own retries."""

    def __init__(self) -> None:
        self.calls = 0

    async def listen(self) -> AsyncIterator[dict[str, str]]:
        self.calls += 1
        if self.calls == 1:
            raise redis.ConnectionError("Redis restarted")
        yield {"type": "pmessage", "channel": keys.channel_tick("EURUSD"), "data": '{"m": 1.08}'}


class TestFanoutRecovery:
    async def test_the_router_resumes_after_a_redis_outage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("fx_api.ws.manager.asyncio.sleep", no_sleep)
        manager = ConnectionManager(redis=None)  # type: ignore[arg-type]
        manager._pubsub = _FlakyPubSub()  # type: ignore[assignment]
        session = _session()
        manager.register(session)

        await manager._router()

        assert [frame["m"] for frame in session.drain()] == [1.08]
