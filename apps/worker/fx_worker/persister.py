"""Redis Stream -> PostgreSQL, with crash recovery.

WHY ASYNCIO AND NOT CELERY
--------------------------
Celery is a *task* queue: discrete jobs, a broker, result backends, serialisation.
Draining a continuous stream is not a task, and routing it through Celery would
mean paying for a second broker and re-implementing delivery semantics that Redis
Streams consumer groups already provide natively (pending list, claim, ack).

Celery/APScheduler still earns its place - for the things it is actually good at:
nightly rollups, partition maintenance, retention. Knowing *when to use which* is
the answer; picking one and using it for everything is not. See ``docs/adr/0004``.

STARTUP ORDER MATTERS
---------------------
    1. XREADGROUP ... STREAMS <stream> 0   -> our OWN pending entries first.
       We may have crashed mid-batch; those entries are ours and nobody else will
       take them until they go idle.
    2. XAUTOCLAIM ... <idle_ms>            -> adopt DEAD workers' entries.
       Without this step, a worker that dies permanently leaves its in-flight
       ticks stranded in the PEL forever - silent, permanent data loss that no
       error log would ever mention.
    3. XREADGROUP ... STREAMS <stream> >   -> steady state: new entries only.

THE ACK ORDERING IS THE ENTIRE GUARANTEE
----------------------------------------
    read -> aggregate -> COMMIT to Postgres -> *then* XACK

Crash before the commit: entries stay pending, get redelivered, get written.
Crash after commit but before XACK: entries get redelivered and are absorbed by
the row-level watermark in ``db.py``. Acking before the commit would convert
every worker crash into silent data loss.

SOURCE OF TRUTH
---------------
The worker recomputes bars from the WAL rather than trusting the ingestor's
in-memory aggregation. That is deliberate: the log is authoritative, so persisted
history is a pure function of it and a replay reproduces it exactly. The
ingestor's in-process bars are a latency optimisation for the live view and can
be lost without consequence. Where they disagree, the log wins.
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
from fx_core.models import Bar
from fx_core.volatility.buckets import BucketAccumulator, floor_to_bucket
from prometheus_client import Counter, Gauge, Histogram

from fx_worker.db import Database

log = structlog.get_logger(__name__)

ROWS_WRITTEN = Counter("fx_bars_written_total", "Bar rows upserted")
TICKS_CONSUMED = Counter("fx_ticks_consumed_total", "Stream entries consumed")
CLAIMED = Counter("fx_entries_claimed_total", "Entries adopted from dead consumers")
MALFORMED = Counter("fx_stream_malformed_total", "Unparseable stream entries")
PENDING = Gauge("fx_pending_entries", "Size of the consumer group PEL")
FLUSH_SIZE = Histogram(
    "fx_flush_batch_size", "Entries per flush", buckets=(1, 10, 50, 100, 250, 500, 1000)
)
FLUSH_SECONDS = Histogram("fx_flush_duration_seconds", "Time to commit one batch")

# How often to sweep for entries orphaned by a worker that died mid-run.
CLAIM_SWEEP_INTERVAL_S = 30.0


class Persister:
    def __init__(
        self,
        redis: aioredis.Redis,
        db: Database,
        consumer_name: str,
        bucket_seconds: int = 60,
        flush_max_rows: int = 500,
        flush_max_seconds: float = 2.0,
        claim_idle_ms: int = 60_000,
    ) -> None:
        self._redis = redis
        self._db = db
        self._consumer = consumer_name
        self._bucket_seconds = bucket_seconds
        self._flush_max_rows = flush_max_rows
        self._flush_max_seconds = flush_max_seconds
        self._claim_idle_ms = claim_idle_ms

        self._accs: dict[tuple[str, int], BucketAccumulator] = {}
        self._last_price: dict[str, float] = {}
        self._max_id: dict[tuple[str, int], str] = {}
        self._unacked: list[str] = []
        self._last_flush = time.monotonic()
        self._stopping = False

    # ------------------------------------------------------------------ setup
    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                keys.STREAM_TICKS, keys.CONSUMER_GROUP, id="0", mkstream=True
            )
            log.info("group.created", group=keys.CONSUMER_GROUP)
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
            log.debug("group.exists", group=keys.CONSUMER_GROUP)

    # --------------------------------------------------------------- parsing
    def _ingest_entry(self, entry_id: str, fields: dict[str, str]) -> None:
        try:
            symbol = fields["s"]
            bid = float(fields["b"])
            ask = float(fields["a"])
            ts = datetime.fromisoformat(fields["t"])
        except (KeyError, ValueError):
            # One corrupt entry must never stall the pipeline. Count it, ack it,
            # move on - a poison message that is retried forever is a worse
            # outage than a dropped tick.
            MALFORMED.inc()
            return

        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        mid = (bid + ask) / 2.0
        bucket = floor_to_bucket(ts, self._bucket_seconds)
        key = (symbol, int(bucket.timestamp()))

        acc = self._accs.get(key)
        if acc is None:
            acc = BucketAccumulator.start(symbol, ts, mid, self._bucket_seconds)
            self._accs[key] = acc

        acc.add(mid, self._last_price.get(symbol))
        self._last_price[symbol] = mid
        # Redis stream ids sort lexicographically the same way they sort
        # chronologically (zero-padded ms-seq), so max() is the watermark.
        prev = self._max_id.get(key)
        if prev is None or entry_id > prev:
            self._max_id[key] = entry_id
        TICKS_CONSUMED.inc()

    def _should_flush(self) -> bool:
        return (
            len(self._unacked) >= self._flush_max_rows
            or (time.monotonic() - self._last_flush) >= self._flush_max_seconds
        )

    # ---------------------------------------------------------------- flushing
    async def flush(self) -> int:
        """Commit, THEN ack. Never the other way round."""
        if not self._accs:
            self._last_flush = time.monotonic()
            return 0

        batch: list[tuple[Bar, str]] = [
            (acc.seal(), self._max_id[key]) for key, acc in self._accs.items()
        ]
        started = time.monotonic()

        try:
            written = await self._db.upsert_bars(batch)
        except Exception:
            # Postgres is down. Do NOT ack: the entries stay in the PEL and the
            # stream keeps buffering for its full retention window, so a short
            # database outage costs latency rather than data. Keep the in-memory
            # accumulators too - retrying re-merges the same statistics, and the
            # watermark makes that safe.
            log.exception("flush.failed", batch=len(batch), unacked=len(self._unacked))
            raise

        if self._unacked:
            await self._redis.xack(keys.STREAM_TICKS, keys.CONSUMER_GROUP, *self._unacked)

        FLUSH_SIZE.observe(len(self._unacked))
        FLUSH_SECONDS.observe(time.monotonic() - started)
        ROWS_WRITTEN.inc(written)
        log.debug("flush.ok", bars=written, entries=len(self._unacked))

        # Keep only the still-open bucket per symbol; sealed ones are durable now.
        now_bucket = int(floor_to_bucket(datetime.now(UTC), self._bucket_seconds).timestamp())
        self._accs = {k: v for k, v in self._accs.items() if k[1] >= now_bucket}
        self._max_id = {k: v for k, v in self._max_id.items() if k in self._accs}
        self._unacked.clear()
        self._last_flush = time.monotonic()
        return written

    # ---------------------------------------------------------------- recovery
    async def drain_own_pending(self) -> int:
        """Step 1: entries this consumer already holds, from a previous life."""
        total = 0
        while True:
            resp = await self._redis.xreadgroup(
                keys.CONSUMER_GROUP,
                self._consumer,
                {keys.STREAM_TICKS: "0"},
                count=self._flush_max_rows,
            )
            entries = self._entries(resp)
            if not entries:
                break
            for entry_id, fields in entries:
                self._ingest_entry(entry_id, fields)
                self._unacked.append(entry_id)
            total += len(entries)
            await self.flush()
        if total:
            log.info("recovery.own_pending_drained", entries=total)
        return total

    async def claim_abandoned(self) -> int:
        """Step 2: adopt entries whose consumer died.

        Without this, a permanently dead worker's in-flight entries sit in the PEL
        forever. Nothing errors; the data is simply never written. That is the
        worst class of bug - invisible, and only discovered as a hole in a chart
        weeks later.
        """
        cursor = "0-0"
        total = 0
        while True:
            result: Any = await self._redis.xautoclaim(
                keys.STREAM_TICKS,
                keys.CONSUMER_GROUP,
                self._consumer,
                min_idle_time=self._claim_idle_ms,
                start_id=cursor,
                count=self._flush_max_rows,
            )
            cursor, entries = result[0], result[1]
            if not entries:
                break
            for entry_id, fields in entries:
                self._ingest_entry(entry_id, fields)
                self._unacked.append(entry_id)
            total += len(entries)
            CLAIMED.inc(len(entries))
            await self.flush()
            if cursor in ("0-0", 0, "0"):
                break
        if total:
            log.warning("recovery.claimed_from_dead_consumers", entries=total)
        return total

    # ------------------------------------------------------------- steady state
    @staticmethod
    def _entries(resp: Any) -> list[tuple[str, dict[str, str]]]:
        if not resp:
            return []
        out: list[tuple[str, dict[str, str]]] = []
        for _stream, items in resp:
            out.extend(items)
        return out

    async def run(self) -> None:
        await self.ensure_group()
        await self.drain_own_pending()
        await self.claim_abandoned()
        log.info("persister.steady_state", consumer=self._consumer)

        claim_check = time.monotonic()

        while not self._stopping:
            resp = await self._redis.xreadgroup(
                keys.CONSUMER_GROUP,
                self._consumer,
                {keys.STREAM_TICKS: ">"},
                count=self._flush_max_rows,
                block=1000,  # ms; returns empty on timeout so the timed flush still fires
            )
            for entry_id, fields in self._entries(resp):
                self._ingest_entry(entry_id, fields)
                self._unacked.append(entry_id)

            if self._should_flush():
                await self.flush()

            # Periodically sweep for entries orphaned by a worker that died while
            # we were running, not just at startup.
            if time.monotonic() - claim_check > CLAIM_SWEEP_INTERVAL_S:
                claim_check = time.monotonic()
                with contextlib.suppress(aioredis.RedisError):
                    await self.claim_abandoned()
                    await self._refresh_pending_metric()

        await self.flush()

    async def _refresh_pending_metric(self) -> None:
        info: Any = await self._redis.xpending(keys.STREAM_TICKS, keys.CONSUMER_GROUP)
        PENDING.set(info.get("pending", 0) if isinstance(info, dict) else 0)

    def stop(self) -> None:
        self._stopping = True


async def run_forever(persister: Persister) -> None:
    """Restart the consume loop on transient infrastructure errors.

    Deliberately does NOT catch everything: a programming error should crash the
    process so the orchestrator restarts it cleanly and the failure is visible,
    rather than being swallowed into a loop that logs forever and ingests nothing.
    """
    backoff = 1.0
    while True:
        try:
            await persister.run()
            return
        except (aioredis.RedisError, OSError) as exc:
            log.warning("persister.retrying", error=str(exc), delay_s=round(backoff, 1))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
