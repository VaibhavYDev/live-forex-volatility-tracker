"""Throughput and backpressure benchmarks.

The README claims numbers. This is where the numbers come from — publish real
measurements or delete the adjective.

Run with:  uv run pytest tests/load -q -s -m load
"""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import UTC, datetime

import pytest
from fx_api.ws.conflator import ClientSession
from fx_core.models import Tick
from fx_core.volatility import BucketAccumulator, EwmaVariance, Welford
from fx_ingestor.providers.replay import ReplayProvider

pytestmark = pytest.mark.load

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"]


def test_volatility_hot_path_is_o1_per_tick() -> None:
    """Per-tick cost must not grow with how long the process has been running.

    A naive sliding window is O(n) per tick, so its per-tick cost climbs steadily
    and the system degrades the longer it stays up — the worst failure shape
    there is, because it passes every short test.
    """
    welford, ewma = Welford(), EwmaVariance(lam=0.97)
    acc = BucketAccumulator.start("EURUSD", datetime.now(UTC), 1.0842, 60)

    def batch(n: int) -> float:
        prev = 1.0842
        start = time.perf_counter()
        for i in range(n):
            price = 1.0842 + (i % 100) * 1e-6
            welford.update(price)
            ewma.update((price - prev) / prev)
            acc.add(price, prev)
            prev = price
        return (time.perf_counter() - start) / n

    first = batch(100_000)  # cost per tick over the first 100k
    for _ in range(8):
        batch(100_000)  # ...800k more ticks of accumulated state...
    last = batch(100_000)  # cost per tick over the last 100k

    print(f"\n  first 100k: {first * 1e9:6.0f} ns/tick")
    print(f"  after 900k: {last * 1e9:6.0f} ns/tick")
    print(f"  ratio:      {last / first:.2f}x   (O(1) => ~1.0, O(n) => grows without bound)")

    assert last < first * 2.0, "per-tick cost grew with state: the hot path is not O(1)"
    assert last < 5e-6, f"hot path too slow: {last * 1e6:.2f} us/tick"


def test_conflation_bounds_memory_under_a_firehose() -> None:
    """The OOM this design prevents, measured."""
    session = ClientSession(client_id="slow")
    session.subscribe(SYMBOLS)

    start = time.perf_counter()
    published = 200_000
    for i in range(published):
        symbol = SYMBOLS[i % len(SYMBOLS)]
        session.offer(f"tick:{symbol}", {"m": 1.0842 + i * 1e-7})
    elapsed = time.perf_counter() - start

    print(
        f"\n  published:  {published:,} updates in {elapsed * 1000:.0f} ms "
        f"({published / elapsed / 1000:.0f}k/s)"
    )
    print(f"  queued:     {len(session.pending)} entries (= subscribed symbols, NOT messages)")
    print(f"  conflated:  {session.conflated:,} superseded before the client read them")
    print(f"  a naive unbounded queue would hold {published:,} entries here")

    assert len(session.pending) == len(SYMBOLS)
    assert session.conflated == published - len(SYMBOLS)


@pytest.mark.anyio
async def test_replay_provider_sustains_target_rate() -> None:
    """The replay feed must actually deliver what it promises, or load numbers lie."""
    target = 2_000.0
    provider = ReplayProvider(SYMBOLS, ticks_per_sec=target, seed=1, burst_every_s=0)
    await provider.connect()

    ticks: list[Tick] = []
    start = time.perf_counter()
    async for tick in provider.stream():
        ticks.append(tick)
        if len(ticks) >= 4_000:
            break
    elapsed = time.perf_counter() - start
    await provider.close()

    rate = len(ticks) / elapsed
    print(f"\n  target:   {target:,.0f} ticks/s")
    print(f"  achieved: {rate:,.0f} ticks/s over {len(ticks):,} ticks")

    assert rate > target * 0.4, f"replay provider only reached {rate:.0f}/s"
    assert all(t.bid < t.ask for t in ticks), "replay produced a crossed book"


@pytest.mark.anyio
async def test_broadcast_latency_across_many_clients() -> None:
    """Fan-out must stay O(clients) with a small constant — no per-client blocking."""
    clients = [ClientSession(client_id=f"c{i}") for i in range(500)]
    for c in clients:
        c.subscribe(SYMBOLS)

    latencies: list[float] = []
    for i in range(2_000):
        frame = {"type": "tick", "s": "EURUSD", "m": 1.0842 + i * 1e-7}
        start = time.perf_counter()
        for c in clients:
            if c.wants("EURUSD"):
                c.offer("tick:EURUSD", frame)
        latencies.append(time.perf_counter() - start)
        if i % 200 == 0:  # a fraction of clients keep up; the rest never read
            for c in clients[:50]:
                c.drain()
        await asyncio.sleep(0)

    p50 = statistics.median(latencies) * 1e6
    p99 = sorted(latencies)[int(len(latencies) * 0.99)] * 1e6
    print(f"\n  broadcast to 500 clients:  p50 {p50:.0f} us   p99 {p99:.0f} us")
    print(f"  slow clients still queued only {len(clients[100].pending)} entries")

    assert p99 < 5_000, f"broadcast p99 {p99:.0f}us — a slow client is blocking the loop"
    assert len(clients[100].pending) <= len(SYMBOLS)
