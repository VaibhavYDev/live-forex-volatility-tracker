# 0009 — `(symbol, seq)` as the alert idempotency key, and what happens if it is lost

**Status:** Accepted · **Date:** 2026-08-22

## Context

Regime transitions travel Redis Streams to Postgres with at-least-once delivery,
exactly like bars. Bars solved idempotency with a row-level stream-id watermark
([ADR-0005](0005-row-level-watermark-idempotency.md)) because they are *merged*
across batches. Transitions are not merged - each is a complete, immutable fact -
so the simpler `ON CONFLICT DO NOTHING` applies, and the only question is what to
conflict on.

## Decision

`UNIQUE (symbol, seq)`, where `seq` is a per-symbol monotonic counter owned by
the `SchmittTrigger` and carried across leader failover in the Redis detector
snapshot.

**Not the Redis stream id.** A stream id deduplicates *redelivery* but not
*re-emission*: after a failover the same logical transition can be XADDed again
under a brand new id, and a stream-id key would happily store it twice. `seq`
identifies the transition itself, which is the thing we actually need to be
unique.

**Not a content hash.** A hash of `(symbol, ts, new_regime)` would also be
collision-proof after state loss (see below), but `seq` buys something a hash
cannot: **gap detection**. Consecutive rows must have consecutive `seq`, so a
dropped transition is a hole a query can find - that is what `alert_regime_gaps`
does. A hash-keyed table cannot tell a missing event from an event that never
happened.

## The failure mode we accept, and how we made it loud

`seq` lives in `regime:{SYMBOL}` in Redis. That key is deliberately durable - no
TTL, so `volatile-lru` cannot evict it, and Redis runs with AOF persistence - but
it is not indestructible. If it is ever lost (a flushed Redis, a restore from an
empty snapshot), the counter restarts at 1 and begins colliding with historical
rows.

A bare `ON CONFLICT DO NOTHING` would then **silently discard every new alert,
forever, with nothing in any log**. That is the worst failure this system has: an
alerting pipeline that reports healthy while alerting nothing.

So the insert uses `RETURNING`, and the persister classifies every conflict:

| stored `ts` vs. incoming `ts` | meaning | response |
|---|---|---|
| identical | redelivery - the guarantee working | `fx_alerts_redelivered_total` |
| different | **`seq` reuse - detector state was lost** | ERROR log + `fx_alerts_seq_collision_total` |

A non-zero collision counter means someone must reseed the counter above the
stored maximum. It is a manual recovery, and it is one we will know about within
one scrape interval instead of one quarter.

## Consequences

- The alternation invariant is checkable in SQL, and is (`alert_regime_gaps`).
- `alert_events` is deliberately **not** a hypertable: TimescaleDB requires every
  UNIQUE constraint to include the partitioning column, which would weaken
  `UNIQUE (symbol, seq)` to `UNIQUE (symbol, seq, ts)` and permit exactly the
  duplicate this ADR exists to prevent. At ~1,700 rows a year that trade is not
  close. See the comment in `003_alert_events.sql`.
- Verified end to end in `tests/integration/test_alert_persistence.py`, including
  the collision path.
