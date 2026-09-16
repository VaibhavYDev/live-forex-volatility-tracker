-- ============================================================================
-- 001_init.sql - schema, hypertable, continuous aggregates, policies
--
-- Runs against the `timescale/timescaledb:2.30.0-pg16` image, which ships the
-- full (TSL-licensed) TimescaleDB, so continuous aggregates, native compression
-- and retention policies are all available. Note the `-oss` variant of that tag
-- is Apache-2 only and would fail on all three.
--
-- NOTE FOR THE SUPABASE PATH (verified August 2026): Supabase ships only the
-- Apache-2 edition of TimescaleDB - no continuous aggregates, no compression -
-- and has DEPRECATED the extension on Postgres 17, recommending native
-- partitioning with pg_partman instead. `002_supabase_partman.sql` provides that
-- alternative. The application code is identical either way because it goes
-- through the repository interface in `fx_worker/db.py`.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------- reference --
CREATE TABLE IF NOT EXISTS instruments (
    symbol      TEXT PRIMARY KEY,
    base_ccy    CHAR(3)       NOT NULL,
    quote_ccy   CHAR(3)       NOT NULL,
    pip_size    NUMERIC(12,8) NOT NULL,   -- 0.0001, or 0.01 for JPY pairs
    display_dp  SMALLINT      NOT NULL DEFAULT 5,
    is_active   BOOLEAN       NOT NULL DEFAULT TRUE
);

INSERT INTO instruments (symbol, base_ccy, quote_ccy, pip_size, display_dp) VALUES
    ('EURUSD', 'EUR', 'USD', 0.0001, 5),
    ('GBPUSD', 'GBP', 'USD', 0.0001, 5),
    ('USDJPY', 'USD', 'JPY', 0.01,   3),
    ('AUDUSD', 'AUD', 'USD', 0.0001, 5),
    ('USDCHF', 'USD', 'CHF', 0.0001, 5),
    ('USDCAD', 'USD', 'CAD', 0.0001, 5),
    ('NZDUSD', 'NZD', 'USD', 0.0001, 5),
    ('EURGBP', 'EUR', 'GBP', 0.0001, 5)
ON CONFLICT (symbol) DO NOTHING;

-- Provenance is a first-class column, not a comment. A chart must be able to
-- show the difference between "this arrived live" and "we fetched this later".
DO $$ BEGIN
    CREATE TYPE bar_source AS ENUM ('stream', 'backfill', 'synthetic');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ------------------------------------------------------------------- bars ----
-- We persist BARS, not raw ticks. EURUSD alone can produce 50 ticks/sec; storing
-- every one is tens of millions of rows a day for data nobody queries at that
-- resolution. Raw ticks live in the capped Redis WAL (15 min) for replay, and
-- `sum_ret`/`sum_ret_sq` preserve exactly what the variance maths needs, so no
-- statistical information is lost by aggregating.
CREATE TABLE IF NOT EXISTS bars_1m (
    symbol         TEXT             NOT NULL REFERENCES instruments(symbol),
    bucket         TIMESTAMPTZ      NOT NULL,
    open           DOUBLE PRECISION NOT NULL,
    high           DOUBLE PRECISION NOT NULL,
    low            DOUBLE PRECISION NOT NULL,
    close          DOUBLE PRECISION NOT NULL,
    tick_count     INTEGER          NOT NULL DEFAULT 0,
    -- Additive sufficient statistics. A 1h variance is the SUM of sixty of
    -- these, so widening a window never revisits a tick.
    sum_ret        DOUBLE PRECISION NOT NULL DEFAULT 0,
    sum_ret_sq     DOUBLE PRECISION NOT NULL DEFAULT 0,
    source         bar_source       NOT NULL DEFAULT 'stream',
    -- Row-level watermark: the highest Redis stream id folded into this bar.
    -- This is what makes a redelivered batch a no-op. See fx_worker/db.py.
    last_stream_id TEXT             NOT NULL DEFAULT '0-0',
    PRIMARY KEY (symbol, bucket)
);

SELECT create_hypertable('bars_1m', 'bucket', chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE, migrate_data => TRUE);

-- BRIN, not B-tree: `bucket` is naturally time-ordered on insert, so BRIN gives
-- almost the same selectivity for a fraction of the size and write cost.
CREATE INDEX IF NOT EXISTS bars_1m_bucket_brin ON bars_1m USING BRIN (bucket);
CREATE INDEX IF NOT EXISTS bars_1m_symbol_bucket ON bars_1m (symbol, bucket DESC);

-- ------------------------------------------------------- continuous aggs ----
-- Incrementally maintained by Timescale: a query for 1h candles never touches
-- the 1m rows. Note the rollups SUM the sufficient statistics, which is why
-- variance at any window is one query and no re-aggregation of ticks.
CREATE MATERIALIZED VIEW IF NOT EXISTS bars_5m
WITH (timescaledb.continuous) AS
SELECT
    symbol,
    time_bucket(INTERVAL '5 minutes', bucket) AS bucket,
    first(open, bucket) AS open,
    max(high)           AS high,
    min(low)            AS low,
    last(close, bucket) AS close,
    sum(tick_count)     AS tick_count,
    sum(sum_ret)        AS sum_ret,
    sum(sum_ret_sq)     AS sum_ret_sq
FROM bars_1m
GROUP BY symbol, time_bucket(INTERVAL '5 minutes', bucket)
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS bars_1h
WITH (timescaledb.continuous) AS
SELECT
    symbol,
    time_bucket(INTERVAL '1 hour', bucket) AS bucket,
    first(open, bucket) AS open,
    max(high)           AS high,
    min(low)            AS low,
    last(close, bucket) AS close,
    sum(tick_count)     AS tick_count,
    sum(sum_ret)        AS sum_ret,
    sum(sum_ret_sq)     AS sum_ret_sq
FROM bars_1m
GROUP BY symbol, time_bucket(INTERVAL '1 hour', bucket)
WITH NO DATA;

-- `end_offset => 1 minute` deliberately leaves the in-progress bucket alone:
-- refreshing a bucket that is still being written produces a candle that changes
-- shape under the user, which reads as a bug.
SELECT add_continuous_aggregate_policy('bars_5m',
    start_offset => INTERVAL '1 hour', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);

SELECT add_continuous_aggregate_policy('bars_1h',
    start_offset => INTERVAL '1 day', end_offset => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);

-- ------------------------------------------------ columnstore + retention ----
-- Compression is opt-in per hypertable. Adding the policy without enabling it
-- first fails with "columnstore not enabled on hypertable", which aborts this
-- script and — when it runs from docker-entrypoint-initdb.d — takes the whole
-- database container down with it.
--
-- TimescaleDB 2.18 renamed compression to the columnstore vocabulary and
-- deprecated add_compression_policy() in favour of add_columnstore_policy(), so
-- the current spelling is used here.
--
-- segmentby = symbol because every read filters on it: grouping a chunk's rows
-- per symbol lets a single-pair query touch one segment instead of all eight.
-- orderby is deliberately left at its default, which is the partitioning column
-- descending — already exactly `bucket DESC`, the order both the writes and the
-- charts use.
ALTER TABLE bars_1m SET (
    timescaledb.enable_columnstore = TRUE,
    timescaledb.segmentby = 'symbol'
);

SELECT add_columnstore_policy('bars_1m', after => INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('bars_1m', INTERVAL '90 days', if_not_exists => TRUE);

-- ----------------------------------------------------------------- alerts ----
CREATE TABLE IF NOT EXISTS alert_rules (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol         TEXT        NOT NULL REFERENCES instruments(symbol),
    window_s       INTEGER     NOT NULL DEFAULT 3600,
    estimator      TEXT        NOT NULL DEFAULT 'ewma',
    enter_z        REAL        NOT NULL DEFAULT 3.0,
    exit_z         REAL        NOT NULL DEFAULT 1.5,
    min_dwell      SMALLINT    NOT NULL DEFAULT 3,
    cooldown_s     INTEGER     NOT NULL DEFAULT 300,
    is_enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Enforce the hysteresis band in the database. A rule with exit_z >= enter_z
    -- reintroduces the alert storm the Schmitt trigger exists to prevent, so it
    -- must be impossible to store one.
    CONSTRAINT hysteresis_band CHECK (exit_z < enter_z)
);

CREATE TABLE IF NOT EXISTS alert_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_id     BIGINT      NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
    symbol      TEXT        NOT NULL,
    kind        TEXT        NOT NULL,   -- fired | cleared | suppressed
    zscore      REAL        NOT NULL,
    sigma       DOUBLE PRECISION NOT NULL,
    reason      TEXT        NOT NULL DEFAULT '',
    fired_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS alert_events_symbol_time ON alert_events (symbol, fired_at DESC);

-- ------------------------------------------------------------ feed health ----
-- Gap ledger: every detected hole, whether or not it was backfillable. A gap you
-- did not record is a gap you will later mistake for a quiet market.
CREATE TABLE IF NOT EXISTS feed_gaps (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol       TEXT        NOT NULL,
    gap_start    TIMESTAMPTZ NOT NULL,
    gap_end      TIMESTAMPTZ NOT NULL,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    backfilled   BOOLEAN     NOT NULL DEFAULT FALSE,
    bars_filled  INTEGER     NOT NULL DEFAULT 0
);
