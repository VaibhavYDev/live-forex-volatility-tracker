-- ============================================================================
-- 002_supabase_partman.sql — the managed-Postgres path.
--
-- Run this INSTEAD OF 001_init.sql when deploying to Supabase or any Postgres
-- without the full (TSL-licensed) TimescaleDB.
--
-- WHY THIS FILE EXISTS (verified August 2026)
-- -------------------------------------------
-- Supabase ships only the Apache-2 edition of TimescaleDB: continuous aggregates
-- and native compression are Community/TSL features and are NOT available. The
-- extension is also DEPRECATED on Postgres 17, and Supabase's own guidance is to
-- migrate hypertables to native partitioning with pg_partman.
--
-- So: same schema, same application code (persistence goes through the repository
-- interface in fx_worker/db.py), different partitioning and rollup mechanism.
--
-- See docs/adr/0007-timescaledb-vs-supabase.md.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- ---------------------------------------------------------------- reference --
CREATE TABLE IF NOT EXISTS instruments (
    symbol      TEXT PRIMARY KEY,
    base_ccy    CHAR(3)       NOT NULL,
    quote_ccy   CHAR(3)       NOT NULL,
    pip_size    NUMERIC(12,8) NOT NULL,
    display_dp  SMALLINT      NOT NULL DEFAULT 5,
    is_active   BOOLEAN       NOT NULL DEFAULT TRUE
);

INSERT INTO instruments (symbol, base_ccy, quote_ccy, pip_size, display_dp) VALUES
    ('EURUSD', 'EUR', 'USD', 0.0001, 5),
    ('GBPUSD', 'GBP', 'USD', 0.0001, 5),
    ('USDJPY', 'USD', 'JPY', 0.01,   3),
    ('AUDUSD', 'AUD', 'USD', 0.0001, 5),
    ('USDCHF', 'USD', 'CHF', 0.0001, 5)
ON CONFLICT (symbol) DO NOTHING;

DO $$ BEGIN
    CREATE TYPE bar_source AS ENUM ('stream', 'backfill', 'synthetic');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ------------------------------------------------- native range partitioning --
CREATE TABLE IF NOT EXISTS bars_1m (
    symbol         TEXT             NOT NULL REFERENCES instruments(symbol),
    bucket         TIMESTAMPTZ      NOT NULL,
    open           DOUBLE PRECISION NOT NULL,
    high           DOUBLE PRECISION NOT NULL,
    low            DOUBLE PRECISION NOT NULL,
    close          DOUBLE PRECISION NOT NULL,
    tick_count     INTEGER          NOT NULL DEFAULT 0,
    sum_ret        DOUBLE PRECISION NOT NULL DEFAULT 0,
    sum_ret_sq     DOUBLE PRECISION NOT NULL DEFAULT 0,
    source         bar_source       NOT NULL DEFAULT 'stream',
    last_stream_id TEXT             NOT NULL DEFAULT '0-0',
    PRIMARY KEY (symbol, bucket)
) PARTITION BY RANGE (bucket);

CREATE INDEX IF NOT EXISTS bars_1m_symbol_bucket ON bars_1m (symbol, bucket DESC);

-- pg_partman maintains daily child partitions and pre-creates four days ahead, so
-- an insert never arrives before its partition exists — the classic native
-- partitioning failure, and the one thing Timescale hid from us.
SELECT public.create_parent(
    p_parent_table    => 'public.bars_1m',
    p_control         => 'bucket',
    p_interval        => '1 day',
    p_premake         => 4
) WHERE NOT EXISTS (
    SELECT 1 FROM public.part_config WHERE parent_table = 'public.bars_1m'
);

UPDATE public.part_config
   SET retention = '90 days', retention_keep_table = false
 WHERE parent_table = 'public.bars_1m';

-- ------------------------------------------- rollups WITHOUT continuous aggs --
-- Plain materialised views, refreshed on a schedule. Timescale would maintain
-- these incrementally; here each refresh re-reads the source window, which is why
-- the views are bounded to recent data. That bound is the actual cost of losing
-- continuous aggregates, and it is worth stating rather than glossing over.
--
-- Note the rollups SUM the sufficient statistics, exactly as the Timescale path
-- does — so variance at any window is one query either way.
CREATE MATERIALIZED VIEW IF NOT EXISTS bars_5m AS
SELECT
    symbol,
    date_trunc('hour', bucket)
        + (floor(extract(minute FROM bucket) / 5) * INTERVAL '5 minutes') AS bucket,
    (array_agg(open  ORDER BY bucket ASC))[1]  AS open,
    max(high)                                  AS high,
    min(low)                                   AS low,
    (array_agg(close ORDER BY bucket DESC))[1] AS close,
    sum(tick_count)                            AS tick_count,
    sum(sum_ret)                               AS sum_ret,
    sum(sum_ret_sq)                            AS sum_ret_sq
FROM bars_1m
WHERE bucket > now() - INTERVAL '7 days'
GROUP BY symbol, 2;

-- UNIQUE index is REQUIRED for REFRESH ... CONCURRENTLY. Without it every refresh
-- takes an ACCESS EXCLUSIVE lock and the dashboard blocks for its duration.
CREATE UNIQUE INDEX IF NOT EXISTS bars_5m_pk ON bars_5m (symbol, bucket);

CREATE MATERIALIZED VIEW IF NOT EXISTS bars_1h AS
SELECT
    symbol,
    date_trunc('hour', bucket)                 AS bucket,
    (array_agg(open  ORDER BY bucket ASC))[1]  AS open,
    max(high)                                  AS high,
    min(low)                                   AS low,
    (array_agg(close ORDER BY bucket DESC))[1] AS close,
    sum(tick_count)                            AS tick_count,
    sum(sum_ret)                               AS sum_ret,
    sum(sum_ret_sq)                            AS sum_ret_sq
FROM bars_1m
WHERE bucket > now() - INTERVAL '90 days'
GROUP BY symbol, 2;

CREATE UNIQUE INDEX IF NOT EXISTS bars_1h_pk ON bars_1h (symbol, bucket);

SELECT cron.schedule('refresh-bars-5m', '* * * * *',
                     'REFRESH MATERIALIZED VIEW CONCURRENTLY bars_5m');
SELECT cron.schedule('refresh-bars-1h', '*/5 * * * *',
                     'REFRESH MATERIALIZED VIEW CONCURRENTLY bars_1h');
SELECT cron.schedule('partman-maintenance', '@hourly',
                     'CALL public.run_maintenance_proc()');

-- ----------------------------------------------------------------- alerts ----
CREATE TABLE IF NOT EXISTS alert_rules (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     UUID        NOT NULL DEFAULT auth.uid(),
    symbol      TEXT        NOT NULL REFERENCES instruments(symbol),
    window_s    INTEGER     NOT NULL DEFAULT 3600,
    estimator   TEXT        NOT NULL DEFAULT 'ewma',
    enter_z     REAL        NOT NULL DEFAULT 3.0,
    exit_z      REAL        NOT NULL DEFAULT 1.5,
    min_dwell   SMALLINT    NOT NULL DEFAULT 3,
    cooldown_s  INTEGER     NOT NULL DEFAULT 300,
    is_enabled  BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT hysteresis_band CHECK (exit_z < enter_z)
);

-- THE reason to pick Supabase for this project: row-level security on user data,
-- for free, instead of hand-rolling a JWT stack.
ALTER TABLE alert_rules ENABLE ROW LEVEL SECURITY;

CREATE POLICY alert_rules_owner ON alert_rules
    FOR ALL USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

-- Market data is public and read-only to clients; only the worker's service role
-- writes it. Enable RLS with a read-only policy rather than leaving it off, so the
-- default is deny.
ALTER TABLE bars_1m ENABLE ROW LEVEL SECURITY;
CREATE POLICY bars_public_read ON bars_1m FOR SELECT USING (true);
