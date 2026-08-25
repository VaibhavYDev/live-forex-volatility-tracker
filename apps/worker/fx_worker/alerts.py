"""Regime transitions: Redis WAL -> PostgreSQL.

Same delivery contract as the tick persister, same ack-after-commit ordering,
but a separate consumer group on a separate stream. Merging them would have been
less code and worse engineering:

* **Retention.** ``stream:ticks`` is trimmed to 15 minutes because it is a crash
  buffer. An alert is the thing a human gets paged about, so it must survive far
  longer - it is trimmed by count, generously, and only as a backstop after
  Postgres has it.
* **Volume.** Ticks arrive tens per second; transitions arrive a few times a
  week. Sharing a stream would bury every alert under six orders of magnitude of
  price data, and every ``XAUTOCLAIM`` recovery sweep would have to walk it.
* **Failure isolation.** A poison tick that stalls the tick persister must not
  also stall alert persistence. These are different blast radii.

No batching timer here either. A transition is rare and important, so it goes to
the database on the next loop iteration rather than waiting for a batch to fill -
the throughput argument that justifies batching bars does not apply to something
that happens twice a week.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
import structlog
from fx_core import keys
from prometheus_client import Counter, Gauge

from fx_worker.db import AlertRow, Database

log = structlog.get_logger(__name__)

ALERTS_WRITTEN = Counter("fx_alerts_written_total", "Regime transitions persisted")
ALERTS_REDELIVERED = Counter(
    "fx_alerts_redelivered_total",
    "Transitions already present - at-least-once delivery working as designed",
)
ALERTS_SEQ_COLLISION = Counter(
    "fx_alerts_seq_collision_total",
    "Transitions dropped because (symbol, seq) was reused - DETECTOR STATE WAS LOST",
)
ALERTS_MALFORMED = Counter("fx_alerts_malformed_total", "Unparseable alert stream entries")
ALERTS_PENDING = Gauge("fx_alerts_pending_entries", "Size of the alert consumer group PEL")

CLAIM_SWEEP_INTERVAL_S = 30.0
_VALID_REGIMES = frozenset({"normal", "stressed"})
_VALID_CAUSES = frozenset({"threshold", "baseline_thaw", "observation_lost"})


def _optional_float(raw: str) -> float | None:
    """Empty string means SQL NULL. ``observation_lost`` has no z-score."""
    if raw == "":
        return None
    value = float(raw)
    return value if value == value and abs(value) != float("inf") else None  # noqa: PLR0124


class AlertPersister:
    def __init__(
        self,
        redis: aioredis.Redis,
        db: Database,
        consumer_name: str,
        claim_idle_ms: int = 60_000,
        batch: int = 100,
    ) -> None:
        self._redis = redis
        self._db = db
        self._consumer = consumer_name
        self._claim_idle_ms = claim_idle_ms
        self._batch = batch
        self._stopping = False

    # ------------------------------------------------------------------ setup
    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                keys.STREAM_ALERTS, keys.ALERT_GROUP, id="0", mkstream=True
            )
            log.info("alert_group.created", group=keys.ALERT_GROUP)
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    # --------------------------------------------------------------- parsing
    def _parse(self, fields: dict[str, str]) -> AlertRow | None:
        """Stream entry -> insertable row, or None (counted) if malformed.

        Validates the enum values here rather than letting Postgres reject the
        whole batch: one corrupt entry must not block every valid transition
        behind it, and a batch that fails forever is a worse outage than a
        dropped record.
        """
        try:
            old_regime = fields["old_regime"]
            new_regime = fields["new_regime"]
            cause = fields["cause"]
            if old_regime not in _VALID_REGIMES or new_regime not in _VALID_REGIMES:
                raise ValueError(f"bad regime: {old_regime!r} -> {new_regime!r}")
            if old_regime == new_regime:
                raise ValueError("not a transition")
            if cause not in _VALID_CAUSES:
                raise ValueError(f"bad cause: {cause!r}")

            ts = datetime.fromisoformat(fields["ts"])
            return AlertRow(
                symbol=fields["s"],
                seq=int(fields["seq"]),
                ts=ts if ts.tzinfo else ts.replace(tzinfo=UTC),
                old_regime=old_regime,
                new_regime=new_regime,
                trigger_value=_optional_float(fields.get("trigger_value", "")),
                threshold_value=float(fields["threshold_value"]),
                sigma=_optional_float(fields.get("sigma", "")),
                cause=cause,
                reason=fields.get("reason", ""),
            )
        except (KeyError, ValueError) as exc:
            ALERTS_MALFORMED.inc()
            log.error("alert.malformed", error=str(exc), fields=fields)
            return None

    # --------------------------------------------------------------- writing
    async def _flush(self, rows: list[AlertRow], entry_ids: list[str]) -> None:
        """Commit, classify any conflicts, THEN ack. Never the other way round."""
        if rows:
            inserted, conflicted = await self._db.insert_alerts(rows)
            ALERTS_WRITTEN.inc(inserted)
            if conflicted:
                await self._classify_conflicts(rows, conflicted)

        if entry_ids:
            await self._redis.xack(keys.STREAM_ALERTS, keys.ALERT_GROUP, *entry_ids)

    async def _classify_conflicts(
        self, rows: list[AlertRow], conflicted: list[tuple[str, int]]
    ) -> None:
        """Distinguish "already stored" from "we just lost an alert".

        A conflict on ``(symbol, seq)`` is normally a redelivered batch - the
        guarantee working as designed, and entirely harmless. But ``seq`` comes
        from a counter that lives in the Redis detector snapshot, and if that
        snapshot is ever lost the counter restarts from 1 and begins colliding
        with historical rows. ``ON CONFLICT DO NOTHING`` would then silently
        discard genuinely new alerts, forever, with no error anywhere.

        Comparing the stored timestamp separates the two cases exactly: a
        redelivery carries the same ts, a collision does not. The collision path
        is loud - ERROR log plus a dedicated counter - because a silent alerting
        failure is the worst failure this system has.
        """
        by_key = {(r["symbol"], r["seq"]): r for r in rows}
        for key in conflicted:
            stored_ts = await self._db.alert_timestamp(*key)
            incoming = by_key[key]
            if stored_ts is not None and stored_ts == incoming["ts"]:
                ALERTS_REDELIVERED.inc()
                continue

            ALERTS_SEQ_COLLISION.inc()
            log.error(
                "alert.seq_collision",
                symbol=key[0],
                seq=key[1],
                stored_ts=stored_ts.isoformat() if stored_ts else None,
                incoming_ts=incoming["ts"].isoformat(),
                detail=(
                    "a DIFFERENT transition already holds this (symbol, seq) - the "
                    "detector snapshot in Redis was lost and the counter restarted. "
                    "This alert was NOT persisted. See docs/adr/0009."
                ),
            )

    # ---------------------------------------------------------------- reading
    @staticmethod
    def _entries(resp: Any) -> list[tuple[str, dict[str, str]]]:
        if not resp:
            return []
        out: list[tuple[str, dict[str, str]]] = []
        for _stream, items in resp:
            out.extend(items)
        return out

    async def _consume(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        rows: list[AlertRow] = []
        ids: list[str] = []
        for entry_id, fields in entries:
            ids.append(entry_id)  # malformed entries are still acked, never retried forever
            row = self._parse(fields)
            if row is not None:
                rows.append(row)
        await self._flush(rows, ids)

    async def drain_own_pending(self) -> int:
        total = 0
        while True:
            resp = await self._redis.xreadgroup(
                keys.ALERT_GROUP, self._consumer, {keys.STREAM_ALERTS: "0"}, count=self._batch
            )
            entries = self._entries(resp)
            if not entries:
                return total
            await self._consume(entries)
            total += len(entries)

    async def claim_abandoned(self) -> int:
        cursor, total = "0-0", 0
        while True:
            result: Any = await self._redis.xautoclaim(
                keys.STREAM_ALERTS,
                keys.ALERT_GROUP,
                self._consumer,
                min_idle_time=self._claim_idle_ms,
                start_id=cursor,
                count=self._batch,
            )
            cursor, entries = result[0], result[1]
            if not entries:
                return total
            await self._consume(entries)
            total += len(entries)
            if cursor in ("0-0", 0, "0"):
                return total

    # ------------------------------------------------------------- main loop
    async def run(self) -> None:
        await self.ensure_group()
        await self.drain_own_pending()
        await self.claim_abandoned()
        log.info("alert_persister.steady_state", consumer=self._consumer)

        claim_check = time.monotonic()
        while not self._stopping:
            resp = await self._redis.xreadgroup(
                keys.ALERT_GROUP,
                self._consumer,
                {keys.STREAM_ALERTS: ">"},
                count=self._batch,
                block=1000,
            )
            entries = self._entries(resp)
            if entries:
                await self._consume(entries)

            if time.monotonic() - claim_check > CLAIM_SWEEP_INTERVAL_S:
                claim_check = time.monotonic()
                with contextlib.suppress(aioredis.RedisError):
                    await self.claim_abandoned()
                    info: Any = await self._redis.xpending(keys.STREAM_ALERTS, keys.ALERT_GROUP)
                    ALERTS_PENDING.set(info.get("pending", 0) if isinstance(info, dict) else 0)

    def stop(self) -> None:
        self._stopping = True


async def run_forever(persister: AlertPersister) -> None:
    """Restart on transient infrastructure errors only.

    Deliberately does not catch everything: a programming error should crash the
    process so the orchestrator restarts it and the failure is visible, rather
    than being swallowed into a loop that logs forever and persists nothing.
    """
    backoff = 1.0
    while True:
        try:
            await persister.run()
            return
        except (aioredis.RedisError, OSError) as exc:
            log.warning("alert_persister.retrying", error=str(exc), delay_s=round(backoff, 1))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
