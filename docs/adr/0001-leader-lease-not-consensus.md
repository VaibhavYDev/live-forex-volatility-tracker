# 0001 — A Redis lease, not consensus, for single-writer election

**Status:** Accepted · **Date:** 2026-08-22

## Context

The moment more than one ingestor replica runs, each opens its own upstream
WebSocket: N× the rate limit, N copies of every tick, N conflicting volatility
numbers. Horizontal scaling breaks the application. We need exactly one replica
connected at a time, with automatic failover.

## Decision

A Redis lease: `SET lock:ingestor:leader <uuid> NX PX 10000`, renewed every 3s by a
Lua compare-and-set. Losing a renewal closes the upstream socket immediately.

Renewal must be a CAS, not `GET` then `PEXPIRE` — the lease could expire and be
acquired by another replica between the two commands, and we would then extend
*someone else's* lease.

`renew_ms` must be well under `ttl_ms / 2` so at least two renewal attempts fit
inside one TTL; otherwise one dropped packet costs leadership while perfectly
healthy. The constructor enforces this and there is a test for it.

## Alternatives rejected

- **Redlock.** More code, more failure modes, and the same fundamental limitation
  Kleppmann identifies ("How to do distributed locking", 2016): no lock service
  without fencing tokens can survive an arbitrarily long pause in the client.
  Implementing it would buy the *appearance* of rigour, not rigour.
- **etcd / ZooKeeper / Consul.** Real consensus, and the correct answer at a
  different scale. Here it means a whole additional stateful system to operate for
  a portfolio project that already runs Redis.
- **A single ingestor replica.** Simplest, but no failover — the feed stops until a
  human notices.

## Consequences — stated, not hidden

**This is a lease, not consensus.** A stop-the-world GC pause longer than the TTL
can produce two leaders for a moment. We do not claim otherwise. Instead the
overlap is made harmless:

- `bars_1m` has `PRIMARY KEY (symbol, bucket)` and the persister upserts, so OHLC
  is unaffected — max, min and last-write-wins are all idempotent.
- The residual cost is a briefly inflated `tick_count` on one bar, which perturbs
  that minute's variance in the third decimal place.

That is the fencing story, achieved with a unique index rather than a distributed
lock. Failover is bounded at the TTL (10s) on an unclean death, and sub-second on a
clean shutdown because the lease is explicitly released.

Related: [ADR-0005](0005-row-level-watermark-idempotency.md).
