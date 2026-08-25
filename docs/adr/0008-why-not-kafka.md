# 0008 — Redis Streams, not Kafka

**Status:** Accepted · **Date:** 2026-08-22

## Context

The ingestion buffer needs an append-only log with replay, consumer groups and
at-least-once delivery. Kafka is the reflexive answer.

## Decision

Redis Streams.

## Rationale

Measure the actual workload before choosing infrastructure sized for a different
one. A dozen FX pairs at 10–50 ticks/sec each is **under 1,000 messages/second and
a few hundred KB/s** — three to four orders of magnitude below where Kafka's design
starts paying for itself.

Redis Streams provides everything this workload needs from a log:

| Requirement | Redis Streams |
|---|---|
| Append-only, replayable | `XADD` / `XRANGE` |
| Consumer groups | `XREADGROUP` |
| At-least-once + explicit pending list | PEL, `XACK` |
| Dead-consumer recovery | `XAUTOCLAIM` |
| Time-based retention | `MINID ~` trimming |

Redis is already in the architecture for the hot cache and the fan-out bus, so this
costs **zero additional operational surface**. Kafka would add a broker (plus
KRaft/ZooKeeper), a schema story, partition and consumer-group rebalancing, and a
second thing to monitor and upgrade — to move 1,000 small messages a second.

## Consequences

Redis Streams is memory-bound: retention is 15 minutes, not 7 days. That is fine,
because the *system of record* is Postgres and the WAL is a crash buffer, not an
archive. If the requirement ever became "replay the last month", the answer would be
object storage plus Parquet, not Kafka.

Kafka becomes the right call at sustained six-figure messages/second, multi-day
replay windows, or many independent consumer teams. We have none of those. Choosing
the smaller tool *for a stated reason* is the decision; reaching for the fashionable
one and being unable to justify it is what this ADR exists to avoid.
