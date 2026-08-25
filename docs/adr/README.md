# Architecture Decision Records

One file per decision that a reviewer might reasonably question. Each states the
context, the decision, the alternatives rejected, and — most importantly — the
consequences we accepted rather than the ones we pretended away.

A decision recorded honestly, limitations included, is worth more than a decision
that looks perfect. These exist because "why didn't you use Kafka?" deserves a
written answer, not a shrug in a code review.

| # | Decision |
|---|---|
| [0001](0001-leader-lease-not-consensus.md) | A Redis lease, not consensus, for single-writer election |
| [0002](0002-redis-eviction-policy.md) | `volatile-lru` with a TTL invariant, not `allkeys-lru` |
| [0003](0003-welford-over-naive-variance.md) | Welford's algorithm for streaming variance |
| [0004](0004-asyncio-not-celery.md) | Asyncio consumer for stream draining; Celery only for scheduled jobs |
| [0005](0005-row-level-watermark-idempotency.md) | Row-level stream-id watermark for exactly-once effect |
| [0006](0006-conflation-for-backpressure.md) | Conflating per-client queues instead of buffering |
| [0007](0007-timescaledb-vs-supabase.md) | Self-hosted TimescaleDB by default; Supabase via pg_partman |
| [0008](0008-why-not-kafka.md) | Redis Streams, not Kafka |
| [0009](0009-alert-idempotency-and-seq-collision.md) | `(symbol, seq)` as the alert idempotency key, and detecting its loss |
