"""Feed status: liveness vs. state age.

THE DEFECT
----------
``_publish_status`` stamped ``ts`` and cached the whole payload; the heartbeat
then wrote that payload back verbatim every 5s - original timestamp included. So
``ts`` meant "when the state last CHANGED", while every consumer read it as "when
we last heard from the feed".

On a feed that connected once and streamed for hours, the two diverge without
limit. The visible result was a dashboard reporting **"Stale · last update 1336s
ago"** over prices that were updating in front of you.

The invisible result was worse. ``/readyz`` applies the same 60s threshold, so
every API replica declared itself unready one minute after a completely healthy
start and stayed that way. Behind a load balancer that is a total outage caused
by nothing being wrong.

The CI compose job only passed because it polls ``/readyz`` for at most 60
seconds and won the race.

This is the exact failure StatusBanner's docstring exists to prevent, inverted:
instead of hiding a problem it invented one, and a banner that cries wolf gets
ignored just as fast as one that stays quiet.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fx_core import keys
from fx_ingestor.config import IngestorSettings
from fx_ingestor.main import Ingestor

pytestmark = pytest.mark.anyio

STALENESS_THRESHOLD_S = 60.0  # /readyz feed_stale_after_s, and the banner's


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeRedis:
    """Records SET and PUBLISH so the heartbeat can be observed without Redis."""

    def __init__(self) -> None:
        self.sets: list[tuple[str, str, int | None]] = []
        self.published: list[tuple[str, str]] = []

    def pipeline(self, transaction: bool = True) -> FakeRedis:
        return self

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.sets.append((key, value, ex))

    def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))

    async def execute(self) -> list[Any]:
        return []


def build(monkeypatch: pytest.MonkeyPatch) -> tuple[Ingestor, FakeRedis]:
    fake = FakeRedis()
    monkeypatch.setattr("fx_ingestor.main.make_redis", lambda _url: fake)
    ing = Ingestor(IngestorSettings(FX_PROVIDER="replay", FX_SYMBOLS="EURUSD"))
    ing.redis = fake  # type: ignore[assignment]
    return ing, fake


def age_of(payload: dict[str, str]) -> float:
    return (datetime.now(UTC) - datetime.fromisoformat(payload["ts"])).total_seconds()


async def run_heartbeats(ing: Ingestor, rounds: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the loop N times, with sleeping replaced so it costs no wall clock."""
    ing.lease._is_leader = True  # type: ignore[attr-defined]

    async def noop_depth() -> None:
        return None

    monkeypatch.setattr(ing.pipeline, "refresh_depth_metric", noop_depth)

    ticks = 0

    async def fake_sleep(_d: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks >= rounds:
            ing._shutdown.set()

    monkeypatch.setattr("fx_ingestor.main.asyncio.sleep", fake_sleep)
    await ing._heartbeat_loop()


async def test_the_heartbeat_restamps_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression, stated as the thing the UI actually asks."""
    ing, fake = build(monkeypatch)
    await ing._publish_status("healthy")

    # Pretend the connection happened well beyond the staleness threshold ago.
    stale = datetime.fromtimestamp(datetime.now(UTC).timestamp() - 3600, tz=UTC).isoformat()
    ing._last_status = {**ing._last_status, "ts": stale}

    await run_heartbeats(ing, rounds=1, monkeypatch=monkeypatch)

    written = json.loads(fake.sets[-1][1])
    assert age_of(written) < 5, (
        "the heartbeat wrote a stale timestamp back; consumers read this as "
        "'no data for an hour' on a feed that never missed a tick"
    )


async def test_a_healthy_feed_never_looks_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end on the number that matters: age must stay under the threshold."""
    ing, fake = build(monkeypatch)
    await ing._publish_status("healthy")
    await run_heartbeats(ing, rounds=20, monkeypatch=monkeypatch)

    ages = [age_of(json.loads(body)) for _k, body, _ex in fake.sets]
    assert ages, "the heartbeat wrote nothing"
    assert max(ages) < STALENESS_THRESHOLD_S


async def test_the_heartbeat_publishes_as_well_as_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser connected an hour ago has no other way to learn we are alive.

    Pub/Sub is the only path to an already-connected client; the stored key only
    serves clients that subscribe later. Storing without publishing leaves every
    open tab's banner to go stale on a feed that is fine.
    """
    ing, fake = build(monkeypatch)
    await ing._publish_status("healthy")
    before = len(fake.published)

    await run_heartbeats(ing, rounds=3, monkeypatch=monkeypatch)

    assert len(fake.published) - before == 3
    assert all(ch == keys.channel_status() for ch, _ in fake.published)


async def test_the_ttl_is_refreshed_every_beat(monkeypatch: pytest.MonkeyPatch) -> None:
    # Absence of the key is how replicas learn the ingestor died without anyone
    # sending a goodbye. A write without the TTL would make it permanent.
    ing, fake = build(monkeypatch)
    await run_heartbeats(ing, rounds=3, monkeypatch=monkeypatch)

    assert fake.sets, "nothing was written"
    for key, _body, ex in fake.sets:
        assert key == keys.FEED_STATUS
        assert ex == keys.TTL_STATUS_S


async def test_a_standby_does_not_write_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two replicas writing conflicting status makes the banner flicker."""
    ing, fake = build(monkeypatch)

    async def noop_depth() -> None:
        return None

    monkeypatch.setattr(ing.pipeline, "refresh_depth_metric", noop_depth)
    ticks = 0

    async def fake_sleep(_d: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks >= 3:
            ing._shutdown.set()

    monkeypatch.setattr("fx_ingestor.main.asyncio.sleep", fake_sleep)
    await ing._heartbeat_loop()  # never became leader

    assert fake.sets == []
    assert fake.published == []


class TestSince:
    """`since` is the field that legitimately does NOT move on a heartbeat."""

    async def test_it_moves_when_the_state_changes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ing, _ = build(monkeypatch)
        await ing._publish_status("healthy")
        first = ing._last_status["since"]

        await asyncio.sleep(0.01)
        await ing._publish_status("degraded", "upstream reconnecting")
        assert ing._last_status["since"] != first

    async def test_it_holds_while_the_state_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "degraded for 12 minutes" is a different and more useful statement than
        # "degraded", and it is only available if this field stays put.
        ing, _ = build(monkeypatch)
        await ing._publish_status("degraded", "first")
        pinned = ing._last_status["since"]

        await asyncio.sleep(0.01)
        await ing._publish_status("degraded", "second attempt")
        assert ing._last_status["since"] == pinned
        assert ing._last_status["ts"] != pinned

    async def test_the_heartbeat_does_not_move_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ing, fake = build(monkeypatch)
        await ing._publish_status("healthy")
        pinned = ing._last_status["since"]

        await run_heartbeats(ing, rounds=5, monkeypatch=monkeypatch)

        for _k, body, _ex in fake.sets:
            assert json.loads(body)["since"] == pinned
