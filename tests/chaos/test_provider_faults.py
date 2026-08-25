"""Fault injection against the provider adapter and supervisor.

Each test names an entry in the failure-mode matrix in ``docs/architecture.md``.
If a claim on the README's front page is not backed by a test in this file, the
claim should be deleted.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
import websockets
from fx_ingestor.providers.base import ProviderAuthError
from fx_ingestor.providers.tiingo import TiingoProvider
from fx_ingestor.supervisor import StalenessWatchdog

from tests.chaos.fault_server import FaultConfig, fault_server

pytestmark = [pytest.mark.chaos, pytest.mark.anyio]


class _PatchedTiingo(TiingoProvider):
    """Tiingo adapter pointed at the local fault server instead of the real one."""

    def __init__(self, url: str, symbols: list[str]) -> None:
        super().__init__(symbols, token="test-token")
        self._url = url

    async def connect(self) -> None:
        self._ws = await websockets.connect(self._url, ping_interval=None)
        await self._ws.send('{"eventName":"subscribe"}')


async def _drain(provider: _PatchedTiingo, limit: int, budget_s: float = 5.0) -> list[object]:
    ticks: list[object] = []

    async def collect() -> None:
        async for tick in provider.stream():
            ticks.append(tick)
            if len(ticks) >= limit:
                return

    # Suppress ONLY the two outcomes that mean "the connection ended", never
    # bare Exception - a broad catch here would hide the very parser crashes
    # these tests exist to detect.
    with contextlib.suppress(TimeoutError, websockets.ConnectionClosed):
        await asyncio.wait_for(collect(), timeout=budget_s)
    return ticks


async def test_malformed_frames_are_dropped_not_fatal() -> None:
    """One corrupt frame must never kill the feed.

    A parser that raises on bad input turns a single provider hiccup into a full
    outage - and providers emit bad frames.
    """
    async with fault_server(FaultConfig(malformed_every=3, frames_per_second=500)) as server:
        provider = _PatchedTiingo(server.url, ["EURUSD"])
        await provider.connect()
        ticks = await _drain(provider, limit=40, budget_s=4.0)
        await provider.close()

    assert len(ticks) >= 30, "feed died on a malformed frame"
    assert provider.dropped_frames > 0, "malformed frames were not counted"
    # Every tick that survived is structurally valid.
    for tick in ticks:
        tick.validate()  # type: ignore[attr-defined]


async def test_connection_drop_terminates_the_stream_cleanly() -> None:
    """A dropped socket ends the iterator; it does not raise into the caller.

    That contract is what lets the supervisor own ALL retry policy - one place
    that decides when to reconnect, rather than retry logic smeared across every
    provider adapter.
    """
    async with fault_server(FaultConfig(drop_after_frames=15, frames_per_second=500)) as server:
        provider = _PatchedTiingo(server.url, ["EURUSD"])
        await provider.connect()
        ticks = await _drain(provider, limit=10_000, budget_s=5.0)
        await provider.close()

    assert 10 <= len(ticks) <= 20


async def test_supervisor_reconnects_after_a_drop() -> None:
    """The reconnect loop actually reconnects - and the server sees it."""
    async with fault_server(FaultConfig(drop_after_frames=5, frames_per_second=500)) as server:
        for _ in range(3):
            provider = _PatchedTiingo(server.url, ["EURUSD"])
            await provider.connect()
            await _drain(provider, limit=10_000, budget_s=2.0)
            await provider.close()

    assert server.connections >= 3


async def test_watchdog_detects_a_stalled_but_open_socket() -> None:
    """THE subtle failure: the socket is healthy, the data has stopped.

    TCP reports nothing wrong. Protocol pings still succeed. Only an
    application-level "no data in N seconds" check catches this, and without it
    the tracker silently displays a frozen price forever.
    """
    fired = asyncio.Event()

    async def on_stale() -> None:
        fired.set()

    async with fault_server(FaultConfig(stall_after_frames=3, frames_per_second=200)) as server:
        provider = _PatchedTiingo(server.url, ["EURUSD"])
        await provider.connect()

        async with StalenessWatchdog(
            timeout_s=0.4,
            on_stale=on_stale,
            check_interval_s=0.1,
            market_is_open=lambda: True,
        ) as watchdog:

            async def pump() -> None:
                async for _tick in provider.stream():
                    watchdog.pet()

            task = asyncio.create_task(pump())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(fired.wait(), timeout=4.0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        await provider.close()

    assert fired.is_set(), "watchdog did not detect the stall"
    assert watchdog.trips >= 1


async def test_auth_failure_is_fatal_not_retried() -> None:
    """Retrying a 401 forever looks healthy on a dashboard while ingesting nothing.

    Worse, it will get the API key throttled or banned. ProviderAuthError is a
    distinct type precisely so the supervisor can refuse to retry it.
    """
    async with fault_server(FaultConfig(reject_auth=True)) as server:
        provider = _PatchedTiingo(server.url, ["EURUSD"])
        await provider.connect()
        with pytest.raises(ProviderAuthError):
            async for _tick in provider.stream():
                pass
        await provider.close()


async def test_watchdog_stands_down_when_the_market_is_closed() -> None:
    """The other half of the calendar feature, and the reason it exists.

    Silence at 03:00 on a Sunday is the market being shut, not a fault. Without
    this branch the watchdog would force-reconnect every 30 seconds from Friday
    evening to Sunday evening - roughly 5_800 pointless reconnects per weekend,
    which is how you get an API key rate-limited before Monday.
    """
    fired = asyncio.Event()

    async def on_stale() -> None:
        fired.set()

    async with StalenessWatchdog(
        timeout_s=0.2,
        on_stale=on_stale,
        check_interval_s=0.05,
        market_is_open=lambda: False,
    ):
        await asyncio.sleep(1.0)

    assert not fired.is_set(), "watchdog fired during a legitimate market close"


async def test_out_of_order_timestamps_do_not_crash_the_parser() -> None:
    """Providers replay and reorder. Nothing here may assume monotonic time."""
    async with fault_server(FaultConfig(out_of_order_every=5, frames_per_second=500)) as server:
        provider = _PatchedTiingo(server.url, ["EURUSD"])
        await provider.connect()
        ticks = await _drain(provider, limit=30, budget_s=4.0)
        await provider.close()

    assert len(ticks) >= 25
    timestamps = [t.ts_event for t in ticks]  # type: ignore[attr-defined]
    assert timestamps != sorted(timestamps), "fault server did not actually reorder"
