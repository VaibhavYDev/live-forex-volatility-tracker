"""Shutdown as Docker asks for it: SIGTERM, then SIGKILL ten seconds later.

THE DEFECT
----------
The signal handler only set a flag, and nothing that blocks looks at it. A standby
polls for the lease every second for as long as the leader lives; a leader waits
in recv() on a socket that has nothing to say all weekend while the market is
closed. Both sat out Docker's grace period and were killed with exit 137, leaving
the lease and the feed status to expire on their own.

Cancelling the work on a requested shutdown unwinds every one of those waits
through the finally blocks that were already there. The status is "stopped" only
when the replica that holds the feed says so: a standby leaving must not tell the
dashboard that someone else's feed has stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fx_ingestor import main as ingestor_main
from fx_ingestor.config import IngestorSettings
from fx_ingestor.main import Ingestor


class SilentProvider:
    """A connected socket on a closed market: it never yields a tick."""

    received_frames = 0
    dropped_frames = 0

    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> SilentProvider:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def close(self) -> None:
        self.closed = True

    async def stream(self) -> AsyncIterator[Any]:
        await asyncio.Event().wait()
        yield  # pragma: no cover - never reached


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, leader: bool) -> None:
        monkeypatch.setattr("fx_ingestor.main.make_redis", lambda _url: None)
        monkeypatch.setattr("fx_ingestor.main.serve_metrics", lambda _port: None)
        monkeypatch.setattr("fx_ingestor.main.configure_logging", lambda *_a: None)
        self.ing = Ingestor(
            IngestorSettings(FX_PROVIDER_TOKEN="t", FX_SYMBOLS="EURUSD", _env_file=None)  # type: ignore[call-arg]
        )
        self.published: list[str] = []
        self.events: list[str] = []
        self.provider = SilentProvider()
        self.handlers: dict[int, Callable[[], None]] = {}

        async def publish(state: str, detail: str = "") -> None:
            self.published.append(state)
            self.events.append(f"status:{state}")

        async def nothing(*_args: object) -> None:
            return None

        async def closed(_redis: object) -> None:
            self.events.append("redis:closed")

        @contextlib.asynccontextmanager
        async def hold() -> AsyncIterator[None]:
            if not leader:
                await asyncio.Event().wait()  # another replica holds the lease
            self.events.append("lease:acquired")
            try:
                yield
            finally:
                self.events.append("lease:released")

        monkeypatch.setattr("fx_ingestor.main.close_redis", closed)
        monkeypatch.setattr(self.ing, "_publish_status", publish)
        monkeypatch.setattr(self.ing, "_heartbeat_loop", nothing)
        monkeypatch.setattr(self.ing, "_rehydrate", nothing)
        monkeypatch.setattr(self.ing, "_build_provider", lambda: self.provider)
        monkeypatch.setattr(self.ing.lease, "hold", hold)
        monkeypatch.setattr("fx_ingestor.main.IngestorSettings", lambda: self.ing.cfg)
        monkeypatch.setattr("fx_ingestor.main.Ingestor", lambda _cfg: self.ing)

    async def start(self) -> asyncio.Task[None]:
        loop = asyncio.get_running_loop()

        def capture(sig: int, handler: Callable[[], None]) -> None:
            self.handlers[sig] = handler

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(loop, "add_signal_handler", capture)
            task = asyncio.create_task(ingestor_main.main())
            for _ in range(50):
                await asyncio.sleep(0)
        assert signal.SIGTERM in self.handlers
        return task


async def test_sigterm_stops_a_standby_waiting_for_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(monkeypatch, leader=False)
    task = await h.start()

    h.handlers[signal.SIGTERM]()
    await asyncio.wait_for(task, 1)  # returns: exit code 0, well inside Docker's 10 s

    # The standby never held the feed, so it says nothing about it.
    assert h.published == []
    assert h.events == ["redis:closed"]


async def test_sigterm_stops_a_leader_on_a_silent_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch, leader=True)
    task = await h.start()
    assert h.published == ["connecting"]  # connected, waiting for a quote that never comes

    h.handlers[signal.SIGTERM]()
    await asyncio.wait_for(task, 1)

    assert h.provider.closed
    # "stopped" is said while the lease is still held, before a successor can speak.
    assert h.events[-3:] == ["status:stopped", "lease:released", "redis:closed"]


async def test_a_leader_that_lost_the_lease_does_not_report_the_feed_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(monkeypatch, leader=True)

    async def lose_then_stop() -> None:
        h.ing.lease.lost.set()  # another replica now owns the feed
        h.ing._shutdown.set()

    monkeypatch.setattr(h.ing, "_run_as_leader", lose_then_stop)
    await asyncio.wait_for(h.ing.run(), 1)

    assert "stopped" not in h.published


async def test_a_cancellation_nobody_requested_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(monkeypatch, leader=True)
    task = await h.start()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
