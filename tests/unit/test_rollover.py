"""The daily 17:00 New York rollover: silence and spread noise that are the market.

WHY THIS EXISTS
---------------
At the 2026-10-25 Sunday open, with a healthy socket, the staleness watchdog forced
four reconnects between 21:00 and 21:03 UTC: every pair was silent for over 30 s and
the feed sends no heartbeat. Forty minutes later the regime detector escalated
GBPUSD to "stressed" on bars whose variance came from 9.5-pip rollover spreads, not
from price moves. Both are the same fact about the market, handled in one place
(fx_core.calendar.in_rollover_window) and checked here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fx_core.alerts import DetectorConfig, RegimeDetector
from fx_core.calendar import in_rollover_window
from fx_ingestor.supervisor import StalenessWatchdog

SUNDAY_OPEN_EDT = datetime(2026, 10, 25, 21, 0, tzinfo=UTC)  # 17:00 New York, daylight time
TUESDAY_ROLL_EST = datetime(2026, 11, 3, 22, 0, tzinfo=UTC)  # 17:00 New York, standard time


class TestRolloverWindow:
    @pytest.mark.parametrize(
        ("ts", "inside"),
        [
            (SUNDAY_OPEN_EDT - timedelta(seconds=1), False),
            (SUNDAY_OPEN_EDT, True),
            (SUNDAY_OPEN_EDT + timedelta(minutes=37), True),  # the GBPUSD escalation
            (SUNDAY_OPEN_EDT + timedelta(minutes=59, seconds=59), True),
            (SUNDAY_OPEN_EDT + timedelta(hours=1), False),
            (TUESDAY_ROLL_EST - timedelta(hours=1), False),  # 21:00 UTC is 16:00 New York in winter
            (TUESDAY_ROLL_EST + timedelta(minutes=10), True),
        ],
    )
    def test_follows_new_york_time_across_daylight_saving(self, ts: datetime, inside: bool) -> None:
        assert in_rollover_window(ts) is inside

    def test_length_is_configurable(self) -> None:
        ts = SUNDAY_OPEN_EDT + timedelta(minutes=45)
        assert in_rollover_window(ts, timedelta(hours=1))
        assert not in_rollover_window(ts, timedelta(minutes=30))
        assert not in_rollover_window(SUNDAY_OPEN_EDT, timedelta(0))


class TestWatchdogAtTheRollover:
    @staticmethod
    async def _trips(in_rollover: bool, quiet_s: float) -> int:
        fired = asyncio.Event()

        async def on_stale() -> None:
            fired.set()

        async with StalenessWatchdog(
            timeout_s=0.2,
            on_stale=on_stale,
            check_interval_s=0.05,
            market_is_open=lambda: True,
            rollover_timeout_s=30.0,
            in_rollover=lambda: in_rollover,
        ) as watchdog:
            await asyncio.sleep(quiet_s)
        return watchdog.trips

    async def test_rollover_silence_is_not_a_dead_socket(self) -> None:
        assert await self._trips(in_rollover=True, quiet_s=0.8) == 0

    async def test_silence_outside_the_rollover_still_forces_a_reconnect(self) -> None:
        assert await self._trips(in_rollover=False, quiet_s=0.8) >= 1

    async def test_without_a_rollover_limit_the_old_limit_applies(self) -> None:
        fired = asyncio.Event()

        async def on_stale() -> None:
            fired.set()

        async with StalenessWatchdog(
            timeout_s=0.2,
            on_stale=on_stale,
            check_interval_s=0.05,
            market_is_open=lambda: True,
            in_rollover=lambda: True,
        ):
            await asyncio.sleep(0.8)
        assert fired.is_set()


class TestDetectorAtTheRollover:
    @staticmethod
    def _warm(detector: RegimeDetector, start: datetime, bars: int = 400) -> datetime:
        ts = start
        for i in range(bars):
            detector.update(1e-4 * (1.0 + 0.1 * ((i * 7) % 5)), ts)
            ts += timedelta(minutes=1)
        return ts

    def test_rollover_bars_are_not_scored(self) -> None:
        detector = RegimeDetector("GBPUSD", market_is_open=lambda _ts: True)
        self._warm(detector, SUNDAY_OPEN_EDT - timedelta(hours=7))
        for i in range(60):
            ts = SUNDAY_OPEN_EDT + timedelta(minutes=i)
            assert detector.update(9.3e-4, ts) is None, f"scored the {ts:%H:%M} rollover bar"
            assert detector.last_zscore is None
            assert detector.trigger.gate_reason == "rollover hour"
        assert not detector.armed

    def test_scoring_resumes_after_the_rollover(self) -> None:
        detector = RegimeDetector("GBPUSD", market_is_open=lambda _ts: True)
        self._warm(detector, SUNDAY_OPEN_EDT - timedelta(hours=7))
        ts = SUNDAY_OPEN_EDT
        for _ in range(60 + DetectorConfig().rearm_bars + 1):
            detector.update(1.1e-4, ts)
            ts += timedelta(minutes=1)
        assert detector.last_zscore is not None
        assert detector.armed

    def test_a_zero_gate_scores_the_rollover(self) -> None:
        detector = RegimeDetector(
            "GBPUSD",
            config=DetectorConfig(rollover_gate=timedelta(0)),
            market_is_open=lambda _ts: True,
        )
        ts = self._warm(detector, SUNDAY_OPEN_EDT - timedelta(hours=7))
        while ts <= SUNDAY_OPEN_EDT + timedelta(minutes=30):
            detector.update(1.1e-4, ts)
            ts += timedelta(minutes=1)
        assert detector.last_zscore is not None

    def test_a_negative_gate_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="rollover_gate"):
            DetectorConfig(rollover_gate=timedelta(minutes=-1))
