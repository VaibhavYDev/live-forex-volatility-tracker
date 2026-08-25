# Live Forex Volatility Tracker — Architecture

> **Thesis:** Anyone can pipe a WebSocket into a chart. This project is about what happens
> when the socket dies mid-tick, when the browser tab can't keep up with the firehose, and
> when you run three API replicas but are only allowed one upstream connection. Those three
> problems are the project.

**Status:** Milestone 0 shipped — working vertical slice, 88 tests green
**Target reviewers:** MLH Fellowship, Google Summer of Code

---

## 0. Design principles

1. **Every component assumes its dependencies will fail.** Reconnection, replay, and
   degraded-mode are first-class features, not error handlers bolted on later.
2. **The hot path is O(1) per tick.** No recomputation over windows, no unbounded buffers,
   no per-message database writes.
3. **Correctness under at-least-once delivery.** We never claim exactly-once; we make
   duplicate delivery *harmless* via idempotent writes.
4. **A stranger can add a data provider in one file.** Provider-specific parsing lives
   behind a `MarketDataProvider` protocol. This is the axis GSoC actually evaluates.
5. **Every claim is instrumented.** If the README says "zero data loss on reconnect,"
   there is a chaos test in CI that kills the socket and asserts it.

### Non-goals

- Trade execution or order management. This is a read-only observability tool.
- Sub-millisecond latency. We target p99 < 250 ms provider→pixel, which is honest for
  a free data tier over the public internet.
- Financial advice. Volatility numbers are displayed with their estimator and window
  labelled, because an unlabelled σ is a lie.

---

## 1. System diagram

```mermaid
flowchart TB
    subgraph EXT[External]
        P[Tiingo FX WebSocket<br/>wss://api.tiingo.com/fx]
        PR[Provider REST<br/>gap backfill]
    end

    subgraph ING[Ingestor - leader-elected, 1 active + N standby]
        SUP[Supervisor<br/>jittered backoff + watchdog]
        ADP[Provider Adapter<br/>positional array to domain Tick]
        AGG[Streaming Aggregator<br/>Welford + EWMA + minute buckets]
    end

    subgraph R[(Redis)]
        ST[stream:ticks<br/>WAL, MINID-trimmed]
        HC[q:SYMBOL / vol:SYMBOL<br/>hot cache, TTL]
        PS[ch:tick:* / ch:vol:*<br/>Pub/Sub fan-out]
        LK[lock:ingestor:leader<br/>lease]
    end

    subgraph W[Persister Worker]
        CG[XREADGROUP consumer group<br/>+ XAUTOCLAIM recovery]
        BAT[Batch to COPY<br/>500 rows or 2s]
    end

    subgraph API[FastAPI - N stateless replicas]
        WS[WS Gateway<br/>conflating per-client queues]
        RST[REST: history, config, alerts]
    end

    subgraph PG[(PostgreSQL)]
        BARS[bars_1m partitioned]
        ROLL[bars_5m / bars_1h rollups]
        META[instruments / alert_rules / alert_events]
    end

    FE[React + Lightweight Charts<br/>rAF-batched imperative updates]

    P -->|ticks| ADP
    SUP -.supervises.-> ADP
    PR -->|backfill on gap| ADP
    ADP --> AGG
    AGG -->|XADD| ST
    AGG -->|HSET| HC
    AGG -->|PUBLISH| PS
    SUP <-->|SET NX PX| LK
    ST --> CG --> BAT -->|COPY + ON CONFLICT DO NOTHING| BARS
    BAT -.XACK after commit.-> ST
    BARS --> ROLL
    PS -->|1 subscription per replica| WS
    HC -->|snapshot on subscribe| WS
    BARS --> RST
    META --> RST
    WS <-->|snapshot then delta| FE
    RST --> FE
```

---

## PART 1 — The four differentiators

### 1.1 The ingestion spine: one writer, a real write-ahead log, and an eviction policy you can defend

**The problem nobody's tutorial solves.** The moment you run more than one FastAPI replica,
each one opens its own upstream WebSocket. You now burn N× your rate limit, ingest N copies
of every tick, and get N conflicting volatility numbers. Horizontal scaling breaks the app.

**The fix — split the ingestor out of the API, and elect a leader.**

```
SET lock:ingestor:leader <instance-uuid> NX PX 10000     # acquire
# renew every 3s via Lua compare-and-set:
#   if redis.call('GET', KEYS[1]) == ARGV[1]
#   then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) else return 0 end
```

If renewal returns 0, the instance **immediately closes its upstream socket** and drops to
standby. Standbys poll for the lease every second, so failover is bounded at ~10 s.

Be honest in the README about what this is: **a lease, not consensus.** A GC pause longer
than the TTL can produce two writers for a moment. We don't pretend otherwise — we make it
*harmless* by deduplicating at the database with a unique index. That is the fencing story,
and saying it out loud is worth more to a reviewer than a fragile Redlock implementation.
(Cite Kleppmann's critique of Redlock in the ADR. Reviewers notice when you've read the
primary sources.)

**Redis Streams as a write-ahead log, not a message queue.**

```
XADD stream:ticks MINID ~ <now_ms - 900000> * s EURUSD b 1.0842 a 1.0844 t 1755859200123 q 3
```

- `MINID ~` trims by **time**, not count — "keep 15 minutes of replay buffer" is a
  requirement you can state in the README; "keep 1,000,000 entries" is not.
- `~` makes trimming approximate: Redis only drops whole macro-nodes, so it's O(1)
  amortized instead of O(n) per write.
- Consumer groups give the persister **at-least-once delivery with an explicit pending
  list**. If the worker dies holding 400 unwritten ticks, they sit in the PEL and the next
  worker claims them. Nothing is lost.

**The eviction question — and the answer most projects get wrong.**

`maxmemory-policy` is a **server-level directive**. Logical databases (`SELECT 1`) do *not*
get separate policies — a very common misconception. And under `allkeys-lru`, Redis can
evict a **whole stream key at once**. It does not politely trim your WAL; it deletes it,
along with every unacknowledged tick in it.

Two defensible configurations:

| Setup | Policy | Trade-off |
|---|---|---|
| **Single instance (recommended here)** | `maxmemory-policy volatile-lru` | Cache keys get TTLs and are evictable. WAL/lease keys carry **no TTL** and are therefore structurally immune. Elegant, one container, and the invariant is enforceable in a lint test. |
| **Two instances (what you'd do at scale)** | `noeviction` for the WAL, `allkeys-lru` for the cache | Stronger isolation, more ops surface. Document it as the production posture. |

Either way, once memory is exhausted writes fail loudly with an OOM error rather than
silently discarding data. Handle that error explicitly: it is **backpressure**, and the
correct response is to stop reading from the upstream socket, not to drop ticks on the
floor. Alert on `used_memory / maxmemory > 0.8`.

**Why not Kafka?** Because at 10–50 ticks/sec across a dozen pairs, Kafka is résumé-driven
development and a reviewer will say so. Redis Streams gives you the log semantics you
actually need at a fraction of the operational weight. *Write that sentence in the ADR* —
demonstrating you rejected the fashionable tool for a reason is a stronger signal than
using it.

---

### 1.2 Streaming volatility: O(1) math, and estimators that show you know the domain

Recomputing a standard deviation over a 5-minute window on every tick is O(n) per tick and
is what every tutorial does. Three layers replace it:

**Layer 1 — Welford's online algorithm** for numerically stable running mean/variance in
O(1), no stored history:

```
n     += 1
delta  = x - mean
mean  += delta / n
M2    += delta * (x - mean)
var    = M2 / (n - 1)
```

Naive `sum(x²) − sum(x)²/n` catastrophically loses precision on FX prices, where the
variance is ~1e-10 of the mean. This is a real correctness fix, not a flourish — measured on
200k simulated EURUSD ticks (σ ≈ 1e-5 around 1.0842):

| Method | Relative error vs. exact |
|---|---|
| Welford | **1.4e-12** |
| Naive sum-of-squares | 8.0e-4 |

Nine orders of magnitude, from four lines of code. Put that table in the ADR.

**Layer 2 — additive minute buckets for sliding windows.** Welford can't cheaply *remove*
old points. So maintain per-minute **sufficient statistics** — `(count, sum, sum_sq, high,
low, open, close)` — which are *additive*. A 5m / 15m / 1h window is then "sum the last
K buckets, evict the oldest," O(K) with K ≤ 60, not O(number of ticks). This is the
subwindow-aggregation trick behind Exponential Histograms / DGIM; cite it in the ADR.

**Layer 3 — EWMA variance (RiskMetrics)** for the headline number, O(1) with *no window
at all*:

```
σ²_t = λ · σ²_{t-1} + (1 − λ) · r²_t        where r_t = ln(P_t / P_{t−1})
```

λ = 0.94 is the RiskMetrics daily convention; for 1-minute bars use λ ≈ 0.97–0.99 and
**state your choice and why** in the docs.

**The domain flex — range-based estimators.** Close-to-close σ throws away the high and low
of every bar. Use the OHLC you're already computing:

```
Parkinson:     σ²_P  = (1 / (4 ln 2)) · ln(H/L)²
Garman-Klass:  σ²_GK = 0.5 · ln(H/L)² − (2 ln 2 − 1) · ln(C/O)²
```

Parkinson is roughly **5× more statistically efficient** than close-to-close for the same
sample size. Verified on 4,000 simulated GBM paths (true σ = 0.01, 400 steps each), measuring
the dispersion of each estimator:

| Estimator | σ̂ recovered | Estimator noise (CV) | Efficiency vs. C2C |
|---|---|---|---|
| Close-to-close | 0.00990 | 1.377 | 1.0× |
| Parkinson | 0.00964 | 0.636 | **≈ 4.7×** |
| Garman-Klass | 0.00954 | 0.534 | **≈ 6.6×** |

(The slight downward bias in the range estimators is the known discrete-sampling bias — the
true high and low fall between observed ticks. Mentioning *that* is the level above knowing
the formula.) Mention **Yang–Zhang** as the extension that handles overnight/weekend gaps —
which FX genuinely has, every Friday 21:00 UTC. Displaying a small estimator selector in the
UI (Close-to-Close / Parkinson / Garman-Klass) with the formula in a tooltip converts the
whole project from "chart app" to "quant tool" in a reviewer's mind, for maybe 80 lines of
code.

**Annualization, stated correctly:** σ_annual = σ_bar · √N, where N = bars per year. FX
trades ~24h × 5d, so ≈ 252 × 24 = 6,048 hours/year → 362,880 one-minute bars. Label every
number in the UI with its estimator, window, and annualization basis. An unlabelled σ is
meaningless and a finance-literate reviewer will notice immediately.

**Alerting with hysteresis.** Fire on a **z-score of current vol against its own trailing
baseline** — that detects a *regime change*, not just a big number:

```
z = (σ_short − mean(σ_long)) / stdev(σ_long)
```

Then add the thing that separates engineers from students: **Schmitt-trigger hysteresis.**
Fire at z > 3.0, but only clear at z < 1.5, plus a minimum dwell time. Without it, a signal
hovering at the threshold generates hundreds of alerts a minute. Add per-rule cooldown and
a dead-man's-switch that suppresses alerts when the feed is stale — because a frozen price
looks like zero volatility, and "volatility collapsed to zero!" is the classic false alarm.

---

### 1.3 Fault tolerance: the part reviewers actually probe

**Reconnect with full jitter**, not naive exponential backoff:

```
sleep = random_between(0, min(cap, base · 2^attempt))
```

Plain exponential backoff synchronizes every client in the world to reconnect at the same
instant when a provider recovers — a thundering herd that re-downs the provider. Full jitter
is AWS's published formula. One line of code, one paragraph in the README, and it shows you
think about the system you're a *client* of.

**A staleness watchdog that knows the FX calendar.** Here is the subtle part: **a dead
socket and a quiet market are byte-for-byte identical.** TCP will happily hold a black-holed
connection open for minutes. So run an application-level watchdog — "no message in 30 s" →
force-close and reconnect.

But naively, that watchdog will reconnect-loop all weekend, because FX **closes Friday
~21:00 UTC and reopens Sunday ~21:00 UTC** (shifting with DST). So the watchdog consults a
market-session calendar and switches to a slow keepalive when the market is closed. Shipping
an FX session calendar is a small file that instantly signals domain competence — almost no
GitHub forex tracker has one.

**Gap detection and backfill with data lineage.** On reconnect, compare the last persisted
bar timestamp against now. If there's a hole, fetch it from the provider's REST endpoint and
insert it **tagged `source = 'backfill'`** rather than `'stream'`. The UI can then render
backfilled regions with a subtle hatch pattern. Provenance tracking in a portfolio project
is a genuine senior-level move — it says you've been burned by untraceable data before.

**Circuit breaker → visible degraded mode.** Closed → Open after N consecutive failures →
Half-Open probes. When Open, the API keeps serving last-known state from Redis *and*
broadcasts `{"type":"status","state":"degraded","since":...}` so the frontend shows a
"Data delayed — reconnecting" banner with the last-good timestamp. Never let a UI silently
display stale prices as if they were live. Honest degradation is a design decision reviewers
remember.

**Prove it with a chaos harness.** `tests/chaos/` runs a local WebSocket server that can be
told to: drop the connection mid-frame, send malformed JSON, stall for 60 s, replay
duplicate sequence numbers, and send out-of-order timestamps. Each has a test asserting the
invariant (zero data loss / no crash / no duplicate rows). **This directory is the single
highest-leverage thing in the repo.** Link it from the README's first screen — it is
verifiable proof of every fault-tolerance claim you make, and it is exactly what nobody
else's forex tracker has.

---

### 1.4 Fan-out and backpressure: the bug that OOMs your server at 3 a.m.

**Fan-out.** Browser connects to replica B; the ingestor runs beside replica A. Solution:
the ingestor `PUBLISH`es to `ch:tick:EURUSD`; **each API replica holds exactly one Redis
subscription** and routes locally to its interested clients. One subscription per *process*,
not per client — with 500 browsers you still have 3 Redis subscribers.

Why both Streams *and* Pub/Sub, when you already have one? Because they solve different
problems and using each for its strength is the senior call:

| | Redis Streams | Redis Pub/Sub |
|---|---|---|
| Delivery | at-least-once, acked, replayable | fire-and-forget |
| Consumer offline | data waits in the PEL | data is gone |
| Cost per browser | a consumer group + polling | free (process-level) |
| Used for | **durability** → Postgres | **fan-out** → browsers |

Persistence must never lose a tick, so it reads the Stream. A browser that missed 40 ms of
ticks doesn't care, so it gets Pub/Sub.

**Backpressure via conflation — the actual differentiator.** A client on hotel wifi, or a
backgrounded tab whose browser throttled its timers, cannot drain 50 msg/sec. The naive
`await ws.send_json(tick)` inside the broadcast loop does one of two catastrophic things:
blocks the broadcast for *every* client, or queues without bound until the process OOMs.

The fix exploits a property of the data: **market prices are last-value-wins.** If three
ticks queue up for a slow client, the first two are worthless — nobody needs to see a price
that's already stale. So each client gets:

```python
class ClientSession:
    pending: dict[str, Tick]      # symbol -> LATEST tick only. Conflation.
    wakeup: asyncio.Event
    dropped: int                  # instrumented, exported to Prometheus
```

The broadcaster does `pending[symbol] = tick; wakeup.set()` — **O(1), never blocks, bounded
by the number of subscribed symbols, not by message rate.** A dedicated writer task per
client drains the map. A slow client automatically receives *decimated* data; a fast one
gets everything. Nobody blocks anybody. Clients whose queue stays saturated past a deadline
get disconnected with a clear close code.

Then **export `dropped` as a metric and put it on the Grafana dashboard.** Being able to say
"here is the graph showing frames conflated for slow clients under load" is the kind of
evidence that ends an interview question in your favour.

Two more that cost little:

- **Server-side subscription filtering.** The client subscribes to 3 pairs; the server sends
  only those 3. Obvious, and routinely skipped.
- **WebSocket auth via short-lived ticket.** The browser `WebSocket` constructor cannot set
  headers, so you *cannot* send `Authorization: Bearer`. Real answer: `POST /ws/ticket`
  returns a 30-second single-use token, passed as a query param and redeemed at handshake.
  Putting a long-lived JWT in a query string leaks it into every access log. Knowing this
  gotcha is a security-literacy signal.

---

### 1.5 The meta-layer (cheap, disproportionately effective)

MLH and GSoC reviewers grade on *contributor experience*, not just code:

- **`docker compose up` → working app with seeded data, one command, no API key required**
  (ship a recorded-tick replay provider as the default). If a reviewer can't run it in 60
  seconds, the architecture doesn't matter.
- **`docs/adr/`** — numbered decision records. `0004-why-not-kafka.md` is more persuasive
  than any amount of code.
- **A load generator** that replays a recorded session at 100× so you can publish a real
  throughput number instead of an adjective.
- **CI**: `ruff` + `mypy --strict` + `pytest` with **testcontainers** for real Redis and
  Postgres. Integration tests against actual services, not mocks.
- **Prometheus + a committed Grafana dashboard JSON.** Screenshot it in the README.
- **`good-first-issue` labels and a CONTRIBUTING.md.** Literally the GSoC rubric.

---

## PART 2 — The blueprint

### 2.1 Monorepo layout

```
forex-volatility-tracker/
├── apps/
│   ├── ingestor/                    # Leader-elected. Owns the ONE upstream connection.
│   │   ├── providers/
│   │   │   ├── base.py              # MarketDataProvider Protocol  <-- the extension point
│   │   │   ├── tiingo.py            # positional-array parser, isolated here
│   │   │   ├── twelvedata.py
│   │   │   └── replay.py            # recorded ticks; makes `docker compose up` key-free
│   │   ├── supervisor.py            # backoff + jitter, watchdog, circuit breaker
│   │   ├── leader.py                # Redis lease acquire/renew/release
│   │   ├── backfill.py              # gap detection -> REST -> source='backfill'
│   │   └── main.py
│   │
│   ├── api/                         # Stateless. Scale to N replicas freely.
│   │   ├── routers/                 # health, instruments, history, alerts, ws
│   │   ├── ws/
│   │   │   ├── manager.py           # ClientSession registry
│   │   │   ├── conflator.py         # the last-value-wins pending map
│   │   │   ├── protocol.py          # subscribe/unsubscribe/snapshot/delta/status
│   │   │   └── tickets.py           # short-lived WS auth tickets
│   │   ├── deps.py
│   │   └── main.py
│   │
│   ├── worker/                      # Redis Stream -> Postgres persister
│   │   ├── persister.py             # XREADGROUP / XAUTOCLAIM / batch COPY / XACK
│   │   ├── rollups.py               # scheduled 1m -> 5m/1h/1d aggregation
│   │   └── retention.py             # partition drop / archive to parquet
│   │
│   └── web/                         # React + Vite + TypeScript
│       ├── src/
│       │   ├── lib/stream/          # reconnecting WS client (same jitter formula)
│       │   ├── hooks/useForexStream.ts
│       │   ├── components/charts/   # LightweightChart wrapper, VolatilityHeatmap (D3)
│       │   └── features/{watchlist,alerts,compare}/
│       └── vite.config.ts
│
├── packages/
│   ├── platform/                    # Boring shared adapters: logging, Redis factory,
│   │                                # metrics server. Has deps, does I/O, knows no domain.
│   ├── core/                        # Pure, dependency-free, 100%-covered domain logic
│   │   ├── models.py                # Tick, Bar, VolSnapshot (pydantic)
│   │   ├── volatility/
│   │   │   ├── welford.py
│   │   │   ├── ewma.py
│   │   │   ├── range_estimators.py  # Parkinson, Garman-Klass, Yang-Zhang
│   │   │   └── buckets.py           # additive sufficient statistics
│   │   ├── alerts/hysteresis.py
│   │   └── calendar/fx_sessions.py  # <-- the file nobody else has
│   └── contracts/
│       ├── schemas/*.json           # single source of truth for the WS protocol
│       └── generate.py              # -> Python models AND TypeScript types
│
├── infra/
│   ├── docker/{Dockerfile.py,Dockerfile.web}
│   ├── redis/redis.conf             # maxmemory + volatile-lru, with the reasoning inline
│   ├── migrations/                  # alembic
│   └── grafana/dashboards/*.json
│
├── tests/
│   ├── unit/                        # core math vs. known-good numpy fixtures
│   ├── integration/                 # testcontainers: real Redis + real Postgres
│   ├── chaos/                       # the fault-injection WS server  <-- headline feature
│   └── load/                        # 100x replay, measures conflation + p99 latency
│
├── docs/
│   ├── adr/0001..000N.md
│   ├── architecture.md
│   └── benchmarks.md                # real numbers, not adjectives
│
├── docker-compose.yml
└── CONTRIBUTING.md
```

**Why `packages/core/` is separate:** all volatility math is pure functions over plain data
— no Redis, no DB, no I/O. It unit-tests in milliseconds against numpy-computed fixtures,
and a contributor can add an estimator without understanding the streaming layer at all.
This split is the single most reviewable decision in the tree.

### 2.2 Redis key namespace

| Key | Type | TTL | Purpose |
|---|---|---|---|
| `stream:ticks` | Stream | none (`MINID ~` 15 min) | Durable WAL → Postgres |
| `cg:persisters` | Consumer group | — | At-least-once + PEL recovery |
| `q:{SYMBOL}` | Hash | 300 s | Last quote — cold-start snapshot |
| `bar:{SYMBOL}:{minute}` | Hash | 3600 s | In-progress bucket (count/sum/sum_sq/OHLC) |
| `vol:{SYMBOL}:{window}` | Hash | 300 s | EWMA + Welford state, all estimators |
| `hist:{SYMBOL}:1m` | Sorted set | 86400 s | Last 1440 bars — snapshot without hitting PG |
| `ch:tick:{SYMBOL}` | Pub/Sub | — | Fan-out to browsers |
| `ch:vol:{SYMBOL}` | Pub/Sub | — | Volatility + alert events |
| `lock:ingestor:leader` | String | 10 s (`PX`) | Single-writer lease |
| `alert:cooldown:{rule}` | String | rule-defined | Hysteresis / rate limiting |

**The invariant to enforce in a test:** everything without a TTL is durable state;
everything with a TTL is disposable. That is exactly what makes `volatile-lru` safe.

### 2.3 PostgreSQL schema (sketch)

```sql
CREATE TABLE instruments (
    symbol       TEXT PRIMARY KEY,          -- 'EURUSD'
    base_ccy     CHAR(3) NOT NULL,
    quote_ccy    CHAR(3) NOT NULL,
    pip_size     NUMERIC(12,8) NOT NULL,    -- 0.0001, or 0.01 for JPY pairs
    is_active    BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TYPE bar_source AS ENUM ('stream', 'backfill', 'synthetic');

CREATE TABLE bars_1m (
    symbol     TEXT        NOT NULL REFERENCES instruments(symbol),
    bucket     TIMESTAMPTZ NOT NULL,           -- minute-truncated
    open       NUMERIC(18,8) NOT NULL,
    high       NUMERIC(18,8) NOT NULL,
    low        NUMERIC(18,8) NOT NULL,
    close      NUMERIC(18,8) NOT NULL,
    tick_count INTEGER     NOT NULL,
    sum_ret    DOUBLE PRECISION NOT NULL,      -- sufficient statistics: additive rollups
    sum_ret_sq DOUBLE PRECISION NOT NULL,      -- without revisiting ticks
    source     bar_source  NOT NULL DEFAULT 'stream',
    PRIMARY KEY (symbol, bucket)                -- <-- idempotency lives here
) PARTITION BY RANGE (bucket);
-- monthly partitions via pg_partman; BRIN index on bucket (naturally time-ordered)
```

`PRIMARY KEY (symbol, bucket)` + `INSERT ... ON CONFLICT (symbol, bucket) DO UPDATE` is what
turns at-least-once delivery into an exactly-once *effect*. Storing `sum_ret` / `sum_ret_sq`
means the 5m/1h/1d rollups are pure addition — you never re-read ticks to compute a longer
window's variance.

**On time-series extensions — verified, August 2026:** Supabase ships only the **Apache-2
edition** of TimescaleDB, which excludes continuous aggregates, compression, and retention
policies; it is **deprecated on Postgres 17** and Supabase's own migration guidance is to
move hypertables to native partitioning with `pg_partman`.

So:

- **Self-hosted (`docker compose`, recommended default):** full TimescaleDB → real
  hypertables, continuous aggregates, native compression.
- **Supabase:** declarative range partitioning + `pg_partman` + materialized views refreshed
  by the worker or `pg_cron`. Its real win is **Auth + RLS**, which saves you writing a JWT
  stack for user watchlists and alert rules.

Design the persister against a repository interface so both work. *And do not use Supabase
Realtime for the price feed* — the WebSocket layer is the thing being evaluated.

### 2.4 Data flow, stage by stage

**Stage 1 — Ingest.** The leader connects to `wss://api.tiingo.com/fx` and subscribes with
its token and `thresholdLevel: 5` (all top-of-book updates). Tiingo returns **positional
arrays**, e.g. `["Q","eurusd","2026-08-22T09:00:00.123Z", bidSize, bid, mid, ask, askSize]` —
the adapter converts that into a domain `Tick` immediately. **Nothing downstream ever sees a
provider-shaped payload.** That anti-corruption boundary is what makes `providers/` a
one-file extension point, and it is the concrete answer to a reviewer asking "how would I add
a second data source?"

**Stage 2 — Aggregate in memory, then write to Redis in one round trip.** The tick updates
in-process Welford/EWMA state (O(1)), then a single pipelined `MULTI`:

```
XADD   stream:ticks MINID ~ <now-15m> * ...      # durability
HSET   q:EURUSD ...                              # snapshot cache
HINCRBY / HSET bar:EURUSD:<minute> ...           # sufficient statistics
PUBLISH ch:tick:EURUSD <compact json>            # fan-out
EXEC
```

One network round trip per tick. Pipelining here rather than four sequential awaits is the
difference between 2 ms and 8 ms of ingest latency.

**Stage 3 — Persist. Asyncio, not Celery.** Celery is a *task* queue — designed for discrete
jobs, with a broker and serialization overhead you'd be paying to duplicate delivery
semantics Redis Streams already gives you. Draining a continuous stream is not a task. Use a
long-lived asyncio consumer:

```
# on startup, in this order:
XREADGROUP GROUP persisters w1 COUNT 500 STREAMS stream:ticks 0     # 1. reclaim OWN pending
XAUTOCLAIM stream:ticks persisters w1 60000 0 COUNT 100             # 2. adopt DEAD workers'
XREADGROUP GROUP persisters w1 COUNT 500 BLOCK 2000 STREAMS ... >   # 3. steady state
```

Batch until **500 rows or 2 seconds, whichever first** (bounded latency *and* bounded
memory), flush with `asyncpg.copy_records_to_table` or a single multi-row upsert, then
**`XACK` only after the transaction commits.** That ordering is the entire at-least-once
guarantee: crash before commit → redelivered; crash after commit but before XACK →
redelivered and absorbed by the primary key.

Keep Celery Beat or APScheduler for what it's actually good at: nightly rollups, partition
maintenance, retention. *Explaining when you would use each is a better answer than picking
one.*

**Stage 4 — Fan-out to browsers, zero polling.** Each API replica opens **one** Redis
`PSUBSCRIBE ch:tick:*` at startup. A browser opens one WebSocket to `/ws/stream` and sends:

```json
{"op": "subscribe", "symbols": ["EURUSD", "GBPJPY"], "windows": ["1m", "15m"]}
```

The server replies **snapshot-then-delta** — the standard market-data pattern:

1. `snapshot`: last quote + last N bars + current vol, read entirely from Redis
   (`q:*`, `hist:*`, `vol:*`) so a new subscriber **never touches Postgres**.
2. `delta`: a stream of compact updates from that point on.

Between the two, the client buffers deltas and replays them after applying the snapshot, so
there is no gap and no double-apply. Interleaved: periodic `status` frames carrying feed
health, so the UI can show degraded state honestly.

**Stage 5 — Render without melting React.** The critical frontend insight: at 50 msg/sec,
`setState` per message means 50 React reconciliations per second and a locked-up main thread.
That single mistake is what makes tutorial clones stutter.

Instead:

- Incoming ticks accumulate in a **`useRef` buffer** — no re-render.
- A `requestAnimationFrame` loop drains the buffer at most once per frame and calls
  Lightweight Charts' **imperative** `series.update()` — which bypasses React entirely and
  repaints on canvas.
- React state is reserved for *structural* change only: symbol added, alert fired, connection
  status changed.
- On `document.visibilitychange` → hidden, send `unsubscribe`. Saves the user's bandwidth and
  your server's CPU, and shows you thought about the whole system.

**Charting split, with a reason:** **Lightweight Charts** for price/candles — canvas-based,
purpose-built for financial series, handles 100k+ bars. **D3** only for the bespoke views it
can't do: a volatility heatmap across pairs, a rolling correlation matrix, a session-overlap
ribbon. Justifying the *split* reads far better than defending a single choice.

### 2.5 Failure-mode matrix

| Failure | Detection | Response | Test |
|---|---|---|---|
| Upstream socket drops | `on_close` / watchdog | Jittered reconnect + REST backfill | `chaos/test_reconnect.py` |
| Upstream silently stalls | 30 s staleness timer (calendar-aware) | Force-close, reconnect | `chaos/test_stall.py` |
| Market legitimately closed | FX session calendar | Slow keepalive, suppress alerts | `unit/test_calendar.py` |
| Malformed provider frame | Pydantic validation at adapter | Log + drop + counter; never crash the loop | `chaos/test_malformed.py` |
| Ingestor process dies | Lease expiry (≤10 s) | Standby promotes | `integration/test_failover.py` |
| Two leaders (GC pause) | — | Harmless: PK dedupe absorbs it | `integration/test_double_write.py` |
| Persister dies mid-batch | PEL entries go idle | `XAUTOCLAIM` by next worker | `chaos/test_worker_kill.py` |
| Postgres down | Connection error | Stop XACK; stream buffers 15 min; alert | `chaos/test_pg_outage.py` |
| Redis memory exhausted | OOM on XADD | Stop reading upstream (backpressure), page | `chaos/test_redis_oom.py` |
| Slow browser client | `pending` map saturated | Conflate; disconnect past deadline | `load/test_slow_client.py` |

Ship this table in the README. It is the most senior-looking artifact in the whole repo,
because it's the table you can only write after you've thought about every edge.

### 2.6 Observability

- **Metrics:** `ingest_lag_seconds` (provider timestamp → ingest), `stream_depth` (`XLEN`),
  `pending_entries` (`XPENDING`), `flush_batch_size`, `ws_clients_connected`,
  `frames_conflated_total`, and an **end-to-end latency histogram** from provider timestamp
  to browser paint. That last one is the headline number for your README.
- **Health:** `/healthz` (process alive) vs `/readyz` (Redis + PG reachable, feed fresh) —
  distinguishing liveness from readiness is a small thing reviewers consistently notice.
- **Logs:** structured JSON with a `correlation_id` threaded from tick → bar → alert.

### 2.7 Suggested milestones

| # | Milestone | Proves |
|---|---|---|
| 0 | Compose skeleton + replay provider + CI | It runs on a reviewer's laptop |
| 1 | Ingestor → Redis Stream → Postgres, with idempotency | The spine works |
| 2 | `packages/core` volatility math + numpy-verified tests | Correctness |
| 3 | WS gateway with snapshot-then-delta + conflation | The hard concurrency |
| 4 | React + Lightweight Charts, rAF-batched | It's real |
| 5 | Chaos suite + Grafana + benchmarks | **The differentiator** |
| 6 | Alerts with hysteresis, multi-estimator UI | The domain flex |

Build 5 *before* polishing the UI. A reviewer with 10 minutes will read the README, run
`docker compose up`, and look for evidence. The chaos suite is that evidence.

---

---

## Implementation status (Milestone 0 shipped)

The scaffold in this repo is a **working vertical slice**, not empty folders:
replay feed -> Redis WAL -> conflating WebSocket -> live Lightweight Charts, plus
the persister writing to Timescale. Verified end to end:

| Gate | Result |
|---|---|
| `ruff check` + `ruff format` | clean |
| `mypy --strict` | clean, 44 source files |
| Unit + integration + chaos tests | **88 passing** (real Redis, not mocks) |
| Frontend `tsc --noEmit` + `vite build` | clean |
| `docker compose config` | valid |
| Exactly-once UPSERT | verified against **PostgreSQL 16** |
| End-to-end smoke | hello -> snapshot -> 86 ticks -> vol -> REST -> /metrics |

Measured, not asserted (`docs/benchmarks.md`):

* volatility hot path **788 ns/tick**, and **0.98x** that cost after 900k ticks -
  the O(1) claim, demonstrated;
* 200,000 updates to one slow client -> **5** entries held in memory;
* broadcast to 500 clients -> **p50 312 us, p99 533 us**.

One design gap the smoke test surfaced and the scaffold fixes: publishing feed
status on Pub/Sub alone leaves a *late-joining* API replica reporting "unknown"
forever, because there is no message it can have missed. Status is now both
published (immediacy) and stored under a TTL'd key (late joiners) - and the TTL's
expiry becomes the ingestor's liveness signal, which is more reliable than a
goodbye a dead process cannot send.

---

## Sources

- [Tiingo Forex WebSocket API documentation](https://www.tiingo.com/documentation/websockets/forex)
- [Supabase — timescaledb extension (deprecation + Apache-2 edition)](https://supabase.com/docs/guides/database/extensions/timescaledb)
- [Supabase issue #12342 — continuous aggregates not supported](https://github.com/supabase/supabase/issues/12342)
- [Twelve Data — WebSocket FAQ](https://support.twelvedata.com/en/articles/5194610-websocket-faq)
- [Finnhub pricing](https://finnhub.io/pricing)
