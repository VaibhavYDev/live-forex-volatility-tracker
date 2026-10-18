-- ============================================================================
-- 004_history_recovery.sql - audit trail for minutes recovered from provider history
--
-- When the feed is down (a sleeping PC, a dropped network, Docker not started),
-- no tick arrives and bars_1m simply has no row for those minutes. The ingestor
-- can now fill them from the provider's own one-minute history. Those rows are
-- not live observations, and this migration is what keeps them distinguishable:
--
--   * bars_1m.source = 'backfill' (the existing enum) says "fetched later";
--   * bars_1m.recovery_run_id says which run fetched it;
--   * history_recovery_runs / history_recovery_spans say what was asked for,
--     what the provider returned, what was inserted and what stayed missing.
--
-- Additive only, and safe to apply to a live database more than once:
--   * no existing row is read, rewritten or deleted;
--   * the new bars_1m column is nullable with no default, so TimescaleDB adds it
--     as catalog metadata - compressed (columnstore) chunks are not rewritten;
--   * no index or foreign key is added to the hypertable, so nothing has to scan
--     or validate existing chunks. recovery_run_id is a soft reference: rows
--     outlive their run record only if someone deletes runs by hand;
--   * the continuous aggregates select named columns, so bars_5m / bars_1h,
--     their refresh policies, the columnstore policy and the 90-day retention
--     policy are untouched.
-- ============================================================================

CREATE TABLE IF NOT EXISTS history_recovery_runs (
    run_id          UUID        PRIMARY KEY,
    trigger         TEXT        NOT NULL,   -- leadership | periodic | manual
    instance        TEXT        NOT NULL,   -- the ingestor instance (or CLI) that ran it
    provider        TEXT        NOT NULL,
    -- The window that was examined: open-market minutes in [window_start, window_end].
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT        NOT NULL DEFAULT 'running',
    minutes_missing INTEGER     NOT NULL DEFAULT 0,   -- open-market minutes with no bar, when the run began
    requests        INTEGER     NOT NULL DEFAULT 0,   -- provider history requests made
    bars_received   INTEGER     NOT NULL DEFAULT 0,   -- provider bars for minutes that were missing
    bars_inserted   INTEGER     NOT NULL DEFAULT 0,   -- rows this run added to bars_1m
    bars_present    INTEGER     NOT NULL DEFAULT 0,   -- minutes stored by someone else before the insert ran
    minutes_silent  INTEGER     NOT NULL DEFAULT 0,   -- requested minutes the provider has no bar for
    cache_added     INTEGER     NOT NULL DEFAULT 0,   -- bars added to the Redis hot cache
    error           TEXT,
    CONSTRAINT history_recovery_runs_status
        CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'aborted')),
    CONSTRAINT history_recovery_runs_trigger
        CHECK (trigger IN ('leadership', 'periodic', 'manual')),
    CONSTRAINT history_recovery_runs_window CHECK (window_start <= window_end)
);

CREATE INDEX IF NOT EXISTS history_recovery_runs_started
    ON history_recovery_runs (started_at DESC);

-- One row per contiguous run of missing minutes that a run asked the provider for.
-- A span the provider answered (recovered, partial or silent) and that was fetched
-- well after its last minute is "settled": its still-missing minutes are minutes the
-- provider does not have (a holiday, a quiet close), and later runs do not ask again.
CREATE TABLE IF NOT EXISTS history_recovery_spans (
    run_id          UUID        NOT NULL REFERENCES history_recovery_runs (run_id) ON DELETE CASCADE,
    symbol          TEXT        NOT NULL,
    span_start      TIMESTAMPTZ NOT NULL,   -- first missing minute (bar start)
    span_end        TIMESTAMPTZ NOT NULL,   -- last missing minute (bar start), inclusive
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT        NOT NULL,
    minutes_missing INTEGER     NOT NULL,
    bars_received   INTEGER     NOT NULL DEFAULT 0,
    bars_inserted   INTEGER     NOT NULL DEFAULT 0,
    bars_present    INTEGER     NOT NULL DEFAULT 0,
    minutes_silent  INTEGER     NOT NULL DEFAULT 0,
    error           TEXT,
    PRIMARY KEY (run_id, symbol, span_start),
    CONSTRAINT history_recovery_spans_status
        CHECK (status IN ('recovered', 'partial', 'silent', 'failed')),
    CONSTRAINT history_recovery_spans_order CHECK (span_start <= span_end)
);

CREATE INDEX IF NOT EXISTS history_recovery_spans_symbol
    ON history_recovery_spans (symbol, span_end DESC);

ALTER TABLE bars_1m ADD COLUMN IF NOT EXISTS recovery_run_id UUID;

COMMENT ON COLUMN bars_1m.recovery_run_id IS
    'history_recovery_runs.run_id of the run that inserted this minute from provider '
    'history; NULL for live (stream) bars and anything stored before migration 004. '
    'Recovered rows carry source = backfill, tick_count = 0 and zero return sums: the '
    'provider gives OHLC only, so no tick count or tick-level variance is known.';
