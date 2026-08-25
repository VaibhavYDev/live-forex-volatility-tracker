# 0005 — Row-level stream-id watermark for exactly-once effect

**Status:** Accepted · **Date:** 2026-08-22

## Context

Redis Streams consumer groups deliver **at-least-once**. If the worker commits to
Postgres and dies before `XACK`, the whole batch is redelivered. That is the
guarantee, not a bug — exactly-once does not exist across two systems.

A plain `ON CONFLICT DO NOTHING` would suffice if bars were immutable. They are
not: a minute's bar is built up across several batches, so the upsert must *merge*.
And merging additive columns (`tick_count`, `sum_ret`, `sum_ret_sq`) is **not**
idempotent — redeliver a batch and you double-count it.

## Decision

Every bar row stores `last_stream_id`: the highest Redis stream id folded into it.
The merge applies only when the incoming batch carries a strictly higher id:

```sql
ON CONFLICT (symbol, bucket) DO UPDATE SET
    high = GREATEST(...), low = LEAST(...), close = EXCLUDED.close,
    tick_count = bars_1m.tick_count + EXCLUDED.tick_count,
    ...
WHERE EXCLUDED.last_stream_id > bars_1m.last_stream_id
```

Stream ids are monotonic and lexicographically ordered, and a batch is one
transaction — so a redelivered batch carries ids at or below the stored watermark
and is skipped wholesale. At-least-once delivery, exactly-once *effect*.

The ordering that makes it work: **read → aggregate → COMMIT → then `XACK`.**
Acking before the commit would convert every worker crash into silent data loss.

## Consequences — the limitation we accept

The watermark does not deduplicate the same *tick* arriving under two different
stream ids, which is what a brief double-leader window produces
([ADR-0001](0001-leader-lease-not-consensus.md)).

Worst case: one minute's `tick_count` is inflated on one bar. OHLC is unaffected
(max, min, last-write-wins are all idempotent) and the variance error is
second-order. We accept this rather than adding a distributed transaction to fix a
sub-10-second edge case that slightly degrades one number.

Tested by `tests/integration/test_redis_pipeline.py::test_redelivery_does_not_double_count`
and `::test_ack_only_after_commit_means_a_crash_loses_nothing`.
