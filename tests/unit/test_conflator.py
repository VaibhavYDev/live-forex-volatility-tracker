"""Backpressure by conflation - the behaviour that keeps the server alive.

These tests exist because "we handle slow clients" is a claim, and a claim
without a test is a wish.
"""

from __future__ import annotations

import time

from fx_api.ws.conflator import ClientSession


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
