# 0002 — `volatile-lru` with a TTL invariant, not `allkeys-lru`

**Status:** Accepted · **Date:** 2026-08-22

## Context

One Redis instance serves three different jobs: a durable write-ahead log
(`stream:ticks`), a disposable hot cache (`q:*`, `bar:*`, `vol:*`, `hist:*`), and a
leader lease. These have opposite durability requirements, and Redis needs a
memory-pressure policy.

## Two facts that decide this

1. **`maxmemory-policy` is a SERVER-level directive.** Logical databases
   (`SELECT 1`) do *not* get separate policies. This is a very common
   misconception, and "I'll put the cache in db 1" does not work.
2. **Under `allkeys-lru`, Redis evicts a whole stream key at once.** It does not
   politely trim the oldest entries of your WAL — it deletes the key, along with
   every entry the persister has not yet acknowledged. Silent, unrecoverable data
   loss, with no error raised anywhere.

## Decision

`maxmemory-policy volatile-lru`, plus an enforced invariant:

> **Keys WITHOUT a TTL are durable. Keys WITH a TTL are disposable.**

`volatile-lru` only considers keys carrying a TTL, so the WAL — which has none — is
*structurally* immune to eviction rather than merely unlikely to be chosen. The
invariant is asserted by `tests/unit/test_resilience.py::TestRedisKeyInvariant` and
by an integration test that checks real TTLs against a live Redis.

Retention is managed explicitly instead: `XADD … MINID ~ <now − 15min>` trims the
WAL by **time**. "Keep 15 minutes of replay buffer" is a requirement you can reason
about during an incident; "keep 1,000,000 entries" means something different at 20
ticks/sec than at 2,000.

## Alternatives rejected

- **`allkeys-lru`** — deletes the WAL under pressure. Disqualifying.
- **`noeviction`** — also correct and safe, but the cache then never sheds anything
  and every key needs manual lifetime management. `volatile-lru` gets the same
  safety plus automatic cache trimming.
- **Two Redis instances** (`noeviction` for the WAL, `allkeys-lru` for the cache) —
  the right production posture, and documented as such. Twice the operational
  surface for a project this size.

## Consequences

Once memory is exhausted and no evictable key remains, writes fail with an OOM
error. That is *correct*: it is backpressure. The ingestor catches it and stops
reading from the upstream socket rather than dropping ticks on the floor. Alert on
`used_memory / maxmemory > 0.8`.

The leader lease does carry a TTL and is therefore technically evictable. Harmless:
losing it means the next standby acquires it, which is exactly the failover path —
and LRU will not choose a 40-byte key over megabytes of quote cache.
