# 0007 — Self-hosted TimescaleDB by default; Supabase via pg_partman

**Status:** Accepted · **Date:** 2026-08-22

## Context

Bars are time-series data needing partitioning, multi-resolution rollups
(1m → 5m → 1h), compression and retention. Two candidate deployments: self-hosted
TimescaleDB in Docker, or managed Supabase.

## What we verified (August 2026)

Supabase ships only the **Apache-2 edition** of TimescaleDB — continuous aggregates
and native compression are Community/TSL features and are *not* available — and the
extension is **deprecated on Postgres 17**, with Supabase's own guidance being to
migrate hypertables to native partitioning with `pg_partman`.

Sources: [Supabase timescaledb docs](https://supabase.com/docs/guides/database/extensions/timescaledb),
[supabase/supabase#12342](https://github.com/supabase/supabase/issues/12342).

## Decision

- **Default (`docker compose`): `timescale/timescaledb:latest-pg16`** — full TSL, so
  hypertables, continuous aggregates, compression and retention policies all work.
- **Supabase path: `infra/migrations/002_supabase_partman.sql`** — declarative range
  partitioning plus materialised views refreshed by the worker or `pg_cron`.

The application is identical either way because persistence goes through the
repository interface in `fx_worker/db.py`.

## Consequences

Supabase's real value here is **Auth + RLS** for user watchlists and alert rules —
not the storage engine. That is a legitimate reason to choose it, and a bad reason
to expect continuous aggregates.

**Do not use Supabase Realtime for the price feed.** It would work, and it would
delete the part of the project being evaluated. The WebSocket gateway, its
conflation and its fan-out *are* the project.
