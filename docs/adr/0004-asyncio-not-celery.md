# 0004 — Asyncio consumer for stream draining; Celery only for scheduled jobs

**Status:** Accepted · **Date:** 2026-08-22

## Context

Ticks must move from the Redis WAL into Postgres continuously, durably, and with
crash recovery.

## Decision

A long-lived asyncio consumer using Redis Streams consumer groups. Celery or
APScheduler is retained only for genuinely scheduled work: nightly rollups,
partition maintenance, retention.

## Rationale

Celery is a *task* queue — designed for discrete jobs with a broker, result
backends and serialisation. Draining a continuous stream is not a task. Routing it
through Celery would mean:

- a second broker to operate, plus serialisation overhead per tick;
- re-implementing delivery semantics that consumer groups already provide natively
  (pending entries list, `XAUTOCLAIM`, `XACK`);
- losing the ability to batch across messages, which is where the throughput is —
  we flush on 500 rows *or* 2 seconds, whichever comes first, giving bounded
  latency and bounded memory. Celery's unit of work is one message.

Knowing *when to use which* is a better answer than picking one tool and using it
everywhere. Both appear in this codebase, each doing what it is good at.

## Consequences

The worker owns its own retry loop and lifecycle rather than inheriting Celery's.
It deliberately does **not** catch every exception: a programming error should crash
the process so the orchestrator restarts it and the failure is visible, rather than
being swallowed into a loop that logs forever and ingests nothing.
