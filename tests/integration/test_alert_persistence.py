"""Regime transitions: Redis WAL -> PostgreSQL, against both real systems.

The chaos suite proves transitions reach the WAL. This proves they get out of it
and into the database exactly once - including the two cases that look identical
from inside the persister and mean opposite things.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
from fx_core import keys
from fx_core.alerts import RegimeDetector
from fx_worker.alerts import (
    ALERTS_MALFORMED,
    ALERTS_REDELIVERED,
    ALERTS_SEQ_COLLISION,
    AlertPersister,
)
from fx_worker.db import Database

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def transition(
    seq: int,
    *,
    symbol: str = "EURUSD",
    to: str = "stressed",
    ts: datetime | None = None,
    cause: str = "threshold",
    z: float | None = 4.2,
) -> dict[str, str]:
    """One entry as the ingestor's ``_transition_payload`` would write it."""
    return {
        "s": symbol,
        "seq": str(seq),
        "ts": (ts or T0 + timedelta(minutes=seq)).isoformat(),
        "old_regime": "normal" if to == "stressed" else "stressed",
        "new_regime": to,
        "trigger_value": "" if z is None else str(z),
        "threshold_value": "3.0" if to == "stressed" else "1.5",
        "sigma": "0.00033",
        "cause": cause,
        "reason": f"seq {seq}",
    }


async def stored(db: Database) -> list[dict[str, object]]:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol, seq, ts, old_regime, new_regime, trigger_value, cause "
            "FROM alert_events ORDER BY symbol, seq"
        )
    return [dict(r) for r in rows]


class TestAlertPersistence:
    async def test_transitions_reach_postgres(self, redis: aioredis.Redis, db: Database) -> None:
        for seq in (1, 2, 3):
            await redis.xadd(
                keys.STREAM_ALERTS,
                transition(seq, to="stressed" if seq % 2 else "normal"),
            )

        persister = AlertPersister(redis, db, consumer_name="w1")
        await persister.ensure_group()
        entries = await redis.xreadgroup(
            keys.ALERT_GROUP, "w1", {keys.STREAM_ALERTS: ">"}, count=10
        )
        await persister._consume(entries[0][1])

        rows = await stored(db)
        assert [r["seq"] for r in rows] == [1, 2, 3]
        assert [str(r["new_regime"]) for r in rows] == ["stressed", "normal", "stressed"]
        pending = await redis.xpending(keys.STREAM_ALERTS, keys.ALERT_GROUP)
        assert pending["pending"] == 0, "entries were not acked after the commit"

    async def test_redelivery_is_a_no_op(self, redis: aioredis.Redis, db: Database) -> None:
        """At-least-once in, exactly-once effect out.

        The same transition delivered twice - which is what a crash between
        COMMIT and XACK produces - must collapse to one row.
        """
        entry = transition(1)
        persister = AlertPersister(redis, db, consumer_name="w1")

        for _ in range(3):
            await persister._consume([("1-1", entry)])

        rows = await stored(db)
        assert len(rows) == 1
        assert rows[0]["seq"] == 1

    async def test_a_reused_seq_is_reported_not_swallowed(
        self, redis: aioredis.Redis, db: Database
    ) -> None:
        """The failure mode ``ON CONFLICT DO NOTHING`` would otherwise hide.

        ``seq`` lives in the Redis detector snapshot. If that snapshot is lost the
        counter restarts at 1 and begins colliding with historical rows - and a
        bare DO NOTHING would then silently discard every genuinely new alert,
        forever, with nothing in the logs.

        Comparing the stored timestamp separates the two cases exactly: a
        redelivery carries the same ts, a collision does not.
        """
        persister = AlertPersister(redis, db, consumer_name="w1")
        await persister._consume([("1-1", transition(1, ts=T0))])

        redeliveries_before = ALERTS_REDELIVERED._value.get()
        collisions_before = ALERTS_SEQ_COLLISION._value.get()

        # Same (symbol, seq), same ts -> redelivery.
        await persister._consume([("1-2", transition(1, ts=T0))])
        # Same (symbol, seq), DIFFERENT ts -> a genuinely different transition.
        await persister._consume([("1-3", transition(1, ts=T0 + timedelta(days=2)))])

        assert ALERTS_REDELIVERED._value.get() == redeliveries_before + 1
        assert ALERTS_SEQ_COLLISION._value.get() == collisions_before + 1
        assert len(await stored(db)) == 1

    async def test_observation_lost_stores_a_null_z_score(
        self, redis: aioredis.Redis, db: Database
    ) -> None:
        """There is no z-score when nothing was observable.

        The schema makes that structural: trigger_value may be NULL only for
        ``observation_lost``. Laundering a NaN into a real-looking number would
        put a fabricated statistic in an audit record.
        """
        await AlertPersister(redis, db, consumer_name="w1")._consume(
            [("1-1", transition(1, to="normal", cause="observation_lost", z=None))]
        )

        rows = await stored(db)
        assert rows[0]["trigger_value"] is None
        assert str(rows[0]["cause"]) == "observation_lost"

    async def test_a_malformed_entry_is_dropped_not_retried_forever(
        self, redis: aioredis.Redis, db: Database
    ) -> None:
        """A poison message retried forever is a worse outage than a lost record."""
        before = ALERTS_MALFORMED._value.get()
        persister = AlertPersister(redis, db, consumer_name="w1")
        await persister._consume(
            [
                ("1-1", {"s": "EURUSD", "seq": "not-a-number"}),
                ("1-2", {**transition(2), "new_regime": "on_fire"}),
                ("1-3", {**transition(3), "old_regime": "normal", "new_regime": "normal"}),
                ("1-4", transition(4)),
            ]
        )

        assert ALERTS_MALFORMED._value.get() == before + 3
        rows = await stored(db)
        assert [r["seq"] for r in rows] == [4], "a valid entry was blocked by invalid ones"

    async def test_a_dead_workers_backlog_is_adopted(
        self, redis: aioredis.Redis, db: Database
    ) -> None:
        """Without XAUTOCLAIM these alerts are lost silently and permanently."""
        for seq in (1, 2):
            await redis.xadd(
                keys.STREAM_ALERTS, transition(seq, to="stressed" if seq % 2 else "normal")
            )

        dead = AlertPersister(redis, db, consumer_name="dead-worker")
        await dead.ensure_group()
        await redis.xreadgroup(keys.ALERT_GROUP, "dead-worker", {keys.STREAM_ALERTS: ">"}, count=10)
        assert (await redis.xpending(keys.STREAM_ALERTS, keys.ALERT_GROUP))["pending"] == 2

        survivor = AlertPersister(redis, db, consumer_name="survivor", claim_idle_ms=0)
        assert await survivor.claim_abandoned() == 2
        assert (await redis.xpending(keys.STREAM_ALERTS, keys.ALERT_GROUP))["pending"] == 0
        assert len(await stored(db)) == 2

    async def test_the_database_refuses_a_broken_alternation(self, db: Database) -> None:
        """The CHECK constraint, and the view that finds gaps the constraint cannot.

        A row-level constraint can reject a self-transition. Only a query across
        rows can spot a MISSING one, which is why alert_regime_gaps exists.
        """
        async with db.pool.acquire() as conn:
            with pytest.raises(Exception, match="alert_events_is_a_transition"):
                await conn.execute(
                    "INSERT INTO alert_events (symbol, seq, ts, old_regime, new_regime, "
                    "trigger_value, threshold_value) "
                    "VALUES ('EURUSD', 1, now(), 'normal', 'normal', 3.0, 3.0)"
                )

            # A well-formed but non-contiguous chain: seq 1 then seq 3.
            for seq, old, new in ((1, "normal", "stressed"), (3, "normal", "stressed")):
                await conn.execute(
                    "INSERT INTO alert_events (symbol, seq, ts, old_regime, new_regime, "
                    "trigger_value, threshold_value) VALUES ($1, $2, now(), $3, $4, 4.0, 3.0)",
                    "EURUSD",
                    seq,
                    old,
                    new,
                )
            gaps = await conn.fetch("SELECT * FROM alert_regime_gaps")

        assert len(gaps) == 1, "the gap view did not spot the dropped transition"
        assert gaps[0]["seq"] == 3
        assert gaps[0]["previous_seq"] == 1


class TestSnapshotRoundTripThroughRedis:
    async def test_detector_snapshot_survives_json(self, redis: aioredis.Redis) -> None:
        """The TypedDicts have to survive the one hop that actually matters.

        A snapshot that round-trips in memory but not through JSON would fail
        only on a real failover - the worst possible time to find out.
        """
        detector = RegimeDetector("EURUSD")
        ts = T0
        for i in range(400):
            detector.update(1e-4 * (1.0 + (i % 7) * 0.05), ts)
            ts += timedelta(minutes=1)

        await redis.set(keys.regime_state("EURUSD"), json.dumps(detector.snapshot()))
        raw = await redis.get(keys.regime_state("EURUSD"))

        revived = RegimeDetector("EURUSD")
        revived.restore(json.loads(raw))

        assert revived.regime is detector.regime
        assert revived.trigger.seq == detector.trigger.seq
        assert revived.baseline.n == detector.baseline.n
        assert revived.baseline.mean == pytest.approx(detector.baseline.mean)
        assert revived.profile.factors() == pytest.approx(detector.profile.factors())
