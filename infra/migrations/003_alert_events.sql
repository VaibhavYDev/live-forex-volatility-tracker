-- ============================================================================
-- 003_alert_events.sql — volatility regime transitions
--
-- Milestone 0 shipped a placeholder `alert_events` shaped around "an alert
-- fired", tied to a user-defined `alert_rules` row. Milestone 1 replaced that
-- model with regime TRANSITIONS emitted by a per-symbol state machine, so the
-- table is replaced rather than migrated: the old one has never held a row, and
-- carrying a `rule_id NOT NULL` foreign key into a system-generated event would
-- mean inventing a fake rule for every transition.
--
-- Forward-only: 001 is not edited. Editing an applied migration is the sin.
-- ============================================================================

DROP VIEW IF EXISTS alert_regime_gaps;
DROP VIEW IF EXISTS regime_current;
DROP TABLE IF EXISTS alert_events;

-- ------------------------------------------------------------------ enums ---
-- Mirrors fx_core.alerts.hysteresis.Regime. Two states, and the machine
-- guarantees they alternate.
DO $$ BEGIN
    CREATE TYPE regime AS ENUM ('normal', 'stressed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Mirrors fx_core.alerts.hysteresis.TransitionCause.
--
-- This column is the reason the alternation invariant survived design review.
-- A bare "stressed -> normal" cannot distinguish three completely different
-- claims, and in a postmortem the difference is the whole story:
--
--   threshold        - the z-score genuinely crossed. The market moved.
--   baseline_thaw    - elevated volatility persisted so long that we re-baselined
--                      and accepted it as the new normal. The market did NOT calm
--                      down; our yardstick moved.
--   observation_lost - we stopped being able to measure. Not a claim about the
--                      market at all.
DO $$ BEGIN
    CREATE TYPE transition_cause AS ENUM ('threshold', 'baseline_thaw', 'observation_lost');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ------------------------------------------------------------------ table ---
CREATE TABLE alert_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol          TEXT             NOT NULL REFERENCES instruments(symbol),

    -- Per-symbol monotonic counter assigned by the SchmittTrigger and carried
    -- across leader failover in the Redis detector snapshot. THIS is the
    -- idempotency key, not the Redis stream id: the same logical transition can
    -- be re-emitted under a new stream id after a failover, so a stream id would
    -- deduplicate redelivery but not re-emission.
    seq             BIGINT           NOT NULL,

    ts              TIMESTAMPTZ      NOT NULL,   -- when the transition committed
    old_regime      regime           NOT NULL,
    new_regime      regime           NOT NULL,
    trigger_value   DOUBLE PRECISION,            -- the z-score that caused it
    threshold_value DOUBLE PRECISION NOT NULL,   -- the threshold it crossed
    sigma           DOUBLE PRECISION,            -- deseasonalised sigma at commit
    cause           transition_cause NOT NULL DEFAULT 'threshold',
    reason          TEXT             NOT NULL DEFAULT '',
    recorded_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),

    -- At-least-once delivery in, exactly-once effect out. A redelivered batch
    -- carries the same (symbol, seq) and collapses to the same row.
    CONSTRAINT alert_events_symbol_seq UNIQUE (symbol, seq),

    -- A "transition" from a state to itself is not a transition. The state
    -- machine cannot emit one; the database refuses to store one anyway,
    -- because a schema that permits impossible rows will eventually hold them.
    CONSTRAINT alert_events_is_a_transition CHECK (old_regime <> new_regime),

    -- trigger_value is NULL only for observation_lost, where there is no
    -- z-score by definition. Making that structural stops a NaN from being
    -- laundered into a real-looking number.
    CONSTRAINT alert_events_trigger_value_presence
        CHECK ((cause = 'observation_lost') OR (trigger_value IS NOT NULL))
);

-- NOT a hypertable, deliberately.
--
-- Eight pairs at a couple of genuine regime changes each per week is roughly
-- 1,700 rows a year. Hypertabling that would be cargo-cult: chunk management
-- and compression policies for a table smaller than most indexes. Worse, it
-- would cost us the constraint above — TimescaleDB requires every UNIQUE
-- constraint on a hypertable to include the partitioning column, so
-- `UNIQUE (symbol, seq)` would have to become `UNIQUE (symbol, seq, ts)`, which
-- permits two rows with the same (symbol, seq) at different timestamps. That is
-- precisely the duplicate the constraint exists to prevent.
--
-- bars_1m IS a hypertable because it takes millions of rows. Applying the same
-- treatment here would be pattern-matching, not engineering. If alert volume
-- ever grew by three orders of magnitude, partition by month with pg_partman and
-- move idempotency to a content hash.

CREATE INDEX alert_events_symbol_ts ON alert_events (symbol, ts DESC);
CREATE INDEX alert_events_open_regimes ON alert_events (symbol, ts DESC)
    WHERE new_regime = 'stressed';

COMMENT ON COLUMN alert_events.seq IS
    'Per-symbol monotonic transition counter. Idempotency key together with symbol. '
    'Restored from the Redis detector snapshot on leader failover; if that snapshot '
    'is ever lost, seq restarts and the persister detects the collision by comparing '
    'ts (see fx_worker.alerts.AlertPersister) rather than silently dropping the row.';

-- ------------------------------------------------------------------ views ---
-- Current regime per symbol. The API serves this in the WebSocket subscribe
-- snapshot so a browser that connects mid-event sees "stressed" immediately,
-- rather than waiting for the next transition to find out.
CREATE VIEW regime_current AS
SELECT DISTINCT ON (symbol)
    symbol, seq, ts, new_regime AS regime, cause, trigger_value, reason
FROM alert_events
ORDER BY symbol, seq DESC;

-- The alternation invariant, as a query.
--
-- Consecutive transitions for a symbol MUST chain: each row's old_regime equals
-- the previous row's new_regime. A row here means an event was dropped between
-- the ingestor and this table — which is exactly the failure that at-least-once
-- delivery plus an idempotency key is supposed to make impossible. Empty is the
-- expected result; anything else is a bug worth paging on.
CREATE VIEW alert_regime_gaps AS
SELECT *
FROM (
    SELECT
        symbol,
        seq,
        ts,
        old_regime                AS expected_previous_regime,
        lag(new_regime) OVER w    AS actual_previous_regime,
        lag(seq) OVER w           AS previous_seq
    FROM alert_events
    WINDOW w AS (PARTITION BY symbol ORDER BY seq)
) chained
WHERE previous_seq IS NOT NULL
  AND (
      -- The chain broke: this row's starting state is not where the previous
      -- row left off, so a transition between them never reached the table.
      actual_previous_regime <> expected_previous_regime
      -- Or the counter skipped, which means the same thing from the other side.
      OR seq <> previous_seq + 1
  );
