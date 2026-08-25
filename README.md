# Live Forex Volatility Tracker

Real-time FX volatility from a streaming tick feed — built around the three problems
that actually make live market data hard: **the socket dies mid-tick**, **the browser
can't keep up with the firehose**, and **you run three API replicas but are only
allowed one upstream connection.**

```bash
git clone <repo> && cd forex-volatility-tracker
cp .env.example .env
docker compose up
```

→ **http://localhost:5173**. No API key, no signup. The default provider is a
deterministic synthetic feed, so a reviewer gets a working dashboard in under a
minute. Set `FX_PROVIDER=tiingo` and `FX_PROVIDER_TOKEN=…` for live data.

---

## What makes this different

Most FX trackers are `websocket → setState → chart`. That works until it doesn't.
Four decisions here are the project:

### 1. One writer, a real write-ahead log, a defensible eviction policy

Run two replicas of a naive ingestor and each opens its own upstream socket: N× the
rate limit, N copies of every tick, N conflicting volatility numbers. Horizontal
scaling *breaks the app*. So the ingestor is a separate service and exactly one
replica holds a Redis lease (`SET … NX PX`, renewed by a Lua compare-and-set).

It's a **lease, not consensus** — a GC pause longer than the TTL can briefly produce
two leaders, and no amount of Lua fixes that. Rather than pretending otherwise, the
overlap is made *harmless* by a row-level watermark in Postgres. That trade is stated
out loud in [ADR-0001](docs/adr/0001-leader-lease-not-consensus.md).

Ticks land in a Redis Stream trimmed with `MINID ~` — retention by **time**
("keep 15 minutes of replay buffer"), not by count, which means something different
at 20 ticks/sec than at 2,000.

**The eviction detail most projects get wrong:** `maxmemory-policy` is a *server*
directive — logical databases do **not** get separate policies — and under
`allkeys-lru` Redis evicts a whole stream key at once. It doesn't trim your WAL, it
deletes it, along with every unacknowledged tick. We use `volatile-lru` with an
invariant enforced by a test: *TTL = disposable, no TTL = durable*.
→ [ADR-0002](docs/adr/0002-redis-eviction-policy.md), [`infra/redis/redis.conf`](infra/redis/redis.conf)

### 2. O(1) volatility, and estimators that show domain literacy

Recomputing a standard deviation over a window on every tick is O(n) per tick. Three
layers replace it — Welford for numerically stable running variance, additive
per-minute sufficient statistics for sliding windows, EWMA for the headline number
with no window at all.

Both claims are measured, not asserted ([`tests/unit/test_volatility.py`](tests/unit/test_volatility.py)):

| | relative error vs. exact |
|---|---|
| **Welford** | **1.4e-12** |
| naive `sum(x²) − sum(x)²/n` | 8.0e-4 |

*(200k simulated EURUSD ticks, σ ≈ 1e-5 around 1.0842. Nine orders of magnitude, from
four lines of code.)*

Close-to-close discards the high and low of every bar. Range estimators don't:

| estimator | σ̂ recovered | estimator noise | efficiency |
|---|---|---|---|
| Close-to-close | 0.00990 | 1.377 | 1.0× |
| Parkinson | 0.00964 | 0.636 | **≈ 4.7×** |
| Garman-Klass | 0.00954 | 0.534 | **≈ 6.6×** |

*(4,000 simulated GBM paths, true σ = 0.01.)* Rogers-Satchell (drift-independent) and
Yang-Zhang (gap-aware — FX gaps every Friday 21:00 UTC) are also implemented and
exposed side by side in the UI. **Every σ is labelled with its estimator, window and
annualisation basis**, because an unlabelled σ is meaningless.

### 2b. Alerting that a quant reviewer can defend

Alerts fire on a **z-score of log volatility against the pair's own trailing
baseline** — a regime change, not an absolute threshold — with
**Schmitt-trigger hysteresis** and symmetric, wall-clock confirmation.

Four things separate it from `if sigma > x: alert()`, each measured:

| | measured effect |
|---|---|
| **Log space.** Realised vol is lognormal, so a linear z-score's "3σ" is not a 1-in-740 event | false-positive rate **13.2× → 1.2×** nominal (~26 → ~2.3 alerts/pair/day) |
| **Freeze the baseline while stressed.** Volatility clustering otherwise stretches the yardstick until the alert clears itself | rolling baseline disarmed after **20 bars** of a regime that never ended; frozen held for 292 |
| **Deseasonalize by hour-of-day.** The London open is not a regime change, it is a clock | **7.3 → 2.7** firings/day, and the 7.3 were all calendar artifacts |
| **Gate the session boundary.** A weekend gap scores z ≈ 11 and fires every Sunday, on schedule | suppressed, with the gap bar excluded from every estimator |

A `TransitionCause` is stored with every event, because "back to normal" means three
different things — the market calmed (`threshold`), we re-baselined and accepted the
new level (`baseline_thaw`), or we stopped being able to measure
(`observation_lost`) — and only one of them is a claim about the market.

Transitions are written to a durable WAL **and** published in the same `MULTI`, so
an alert that is recorded is also broadcast. Detector state is snapshotted to Redis
every sealed bar so a promoted standby continues an event rather than losing it.
→ [ADR-0009](docs/adr/0009-alert-idempotency-and-seq-collision.md)

### 3. Fault tolerance you can verify

- **Full-jitter backoff** — `random(0, min(cap, base·2^n))`. Plain exponential backoff
  synchronises every client on Earth to reconnect at the same instant when a provider
  recovers. Same formula in the browser client, for the same reason.
- **A staleness watchdog that knows the FX calendar.** A dead socket and a quiet
  market are byte-for-byte identical; TCP will hold a black-holed connection open for
  minutes. So: "no data in 30s → force reconnect" — but the FX market closes Friday
  17:00 New York and reopens Sunday 17:00, so the watchdog consults a session calendar
  and stands down. Without it: ~5,800 pointless reconnects every weekend.
  ([`fx_core/calendar/fx_sessions.py`](packages/core/fx_core/calendar/fx_sessions.py) —
  anchored in `America/New_York` so DST is the stdlib's problem, not ours.)
- **Gap backfill with provenance.** Holes are refilled from REST and tagged
  `source='backfill'`, and the chart renders them distinctly. A number you can't trace
  is a number you can't trust.
- **Circuit breaker → visible degraded mode.** The UI shows "Data delayed —
  reconnecting" with the last-good timestamp. Silently rendering stale prices as live
  is the one thing a market dashboard must never do.

### 4. Backpressure by conflation

A backgrounded tab can't drain 50 msg/sec. `await ws.send_json(tick)` in a broadcast
loop either blocks every other client or grows without bound until the process is
OOM-killed.

Market prices are **last-value-wins**, so each client holds a `dict[symbol, payload]`
containing only the newest update. Publishing is O(1) and never blocks; memory is
bounded by *subscribed symbols*, not message rate. Slow clients silently receive
decimated but always-current data.

Measured ([`docs/benchmarks.md`](docs/benchmarks.md)):

```
200,000 updates published  →  5 entries held in memory (= subscribed symbols)
                           →  199,995 conflated (all of them stale prices)
broadcast to 500 clients   →  p50 312 µs, p99 533 µs — nobody blocks anybody
volatility hot path        →  788 ns/tick, and 0.98× the cost after 900k ticks
```

That last ratio is the O(1) claim: a naive sliding window's per-tick cost climbs
with uptime, which passes every short test and degrades in production after hours.

Conflation is safe *because* the data is last-value-wins — it would be wrong for an
order stream. Knowing which kind of stream you have is the engineering.
→ [`fx_api/ws/conflator.py`](apps/api/fx_api/ws/conflator.py),
[`tests/unit/test_conflator.py`](tests/unit/test_conflator.py)

---

## The chaos suite

Every fault-tolerance claim above has a test that proves it. `tests/chaos/` runs a
WebSocket server that misbehaves on command — you cannot ask Tiingo to drop your
connection mid-frame.

```bash
make test-chaos
```

| Injected fault | Asserted invariant |
|---|---|
| Malformed JSON frames | Feed survives; frames counted, not fatal |
| Connection dropped mid-stream | Stream ends cleanly; supervisor owns all retry |
| Socket stalls but stays open | Watchdog trips (the failure TCP can't see) |
| Market closed | Watchdog **stands down** — silence isn't a fault |
| Auth rejected | Fatal, never retried (a 401 retry loop gets keys banned) |
| Out-of-order timestamps | Parser unaffected; nothing assumes monotonic time |
| Postgres outage mid-batch | **No `XACK`** → entries stay pending → zero loss |
| Worker killed holding entries | `XAUTOCLAIM` adopts them |
| Batch redelivered | Watermark makes it a no-op — no double-count |
| Two ingestors racing | Only one holds the lease |
| Noisy sine-wave volatility | One escalation per macro cycle, none from the noise |
| Leader killed mid-event | Standby rehydrates; no duplicate, and the clear still fires |
| Corrupt detector snapshot | Starts cold and logs it, rather than refusing to start |
| Reused alert `seq` after state loss | Detected and reported, never silently dropped |

---

## Architecture

```
Tiingo WS ──► Ingestor (leader-elected, 1 of N active)
                 │  Welford + EWMA, O(1)/tick
                 ▼
          ┌─────────────── Redis ───────────────┐
          │ stream:ticks   WAL, MINID-trimmed   │──► Worker ──► TimescaleDB
          │ q:* hist:* vol:* zhist:*  cache(TTL)│    XREADGROUP    bars_1m
          │ ch:tick:*      Pub/Sub fan-out      │    → batch       + continuous
          │ lock:…:leader  lease                │    → COPY        aggregates
          └──────────────────┬──────────────────┘    → XACK
                             ▼
                 FastAPI × N (stateless)
                 one Redis subscription per replica
                 per-client conflating queues
                             │  snapshot-then-delta
                             ▼
                 React + Lightweight Charts
                 rAF-batched imperative updates
```

**Streams *and* Pub/Sub, each for its strength:** persistence must never lose a tick,
so it reads the Stream (at-least-once, acked, replayable). A browser that missed 40ms
doesn't care and would rather have the newest price, so it gets Pub/Sub (free at the
process level — one subscription per *replica*, not per client).

Full design: [`ARCHITECTURE.md`](ARCHITECTURE.md) · Decisions: [`docs/adr/`](docs/adr/)

---

## Layout

```
packages/core/       Pure domain logic. ZERO dependencies, zero I/O.
                     Volatility maths, models, FX session calendar, Redis key
                     namespace. Tests run in milliseconds; a contributor can add
                     an estimator without reading the streaming layer.
packages/platform/   Boring shared adapters: logging, Redis factory, metrics.
apps/ingestor/       Leader-elected. Owns the ONE upstream connection.
                     providers/base.py is the extension point — add a data source
                     in one file plus one registry line.
apps/worker/         Redis Stream → Postgres. At-least-once in, exactly-once out.
apps/api/            Stateless FastAPI. REST + conflating WebSocket gateway.
apps/web/            React + Vite + Lightweight Charts.
tests/chaos/         Fault injection. The evidence for everything above.
```

---

## Development

```bash
make install     # uv sync --all-packages + npm install
make dev         # redis + timescale in docker, services with hot reload
make test        # unit + integration + chaos
make lint        # ruff + mypy --strict + tsc --noEmit
make bench       # throughput + backpressure benchmarks (docs/benchmarks.md)
make up          # full stack in docker
make observe     # + Prometheus and Grafana on :3000
```

Requires [uv](https://docs.astral.sh/uv/), Node 20+, Docker.

**Quality gates in CI:** `ruff` · `mypy --strict` (zero `Any` escapes) · `pytest`
against real Redis and Postgres via testcontainers, not mocks — mocking Redis would
mean mocking the exact consumer-group semantics under test.

### Adding a data provider

1. Implement `MarketDataProvider` in `apps/ingestor/fx_ingestor/providers/`.
2. Add one line to `_REGISTRY` in `providers/__init__.py`.

Nothing else changes. Provider wire formats are converted to the domain `Tick` at that
boundary, so no provider-shaped payload ever reaches the volatility maths, the WAL or
the schema. (Tiingo sends *positional arrays* — exactly the kind of format that breaks
naive consumers silently when a field is inserted.)

---

## Observability

`make observe`, then Grafana on :3000.

| Metric | Why it matters |
|---|---|
| `fx_ingest_lag_seconds` | Provider timestamp → our process |
| `fx_stream_depth` / `fx_pending_entries` | WAL backlog and unacked entries |
| `fx_ws_frames_conflated_total` | Backpressure, working, as a graph |
| `fx_entries_claimed_total` | Recovery from a dead worker actually happening |
| `fx_bars_written_total` | Durable throughput |

`/healthz` (liveness — checks nothing external) is deliberately distinct from
`/readyz` (readiness — Redis reachable *and* the ingestor's heartbeat key alive). A
liveness probe that pings Redis restarts every healthy pod during a Redis blip,
turning a partial outage into a total one.

---

## Database

The Docker path uses **full TimescaleDB** — hypertables, continuous aggregates,
native compression, retention policies.

**On Supabase (verified August 2026):** Supabase ships only the *Apache-2* edition of
TimescaleDB (no continuous aggregates, no compression) and has **deprecated** the
extension on Postgres 17, recommending native partitioning with `pg_partman`.
`infra/migrations/002_supabase_partman.sql` is that path. Supabase's real value here is
Auth + RLS for user watchlists and alert rules — not the storage engine. Don't use
Supabase Realtime for the price feed: the WebSocket layer *is* the project.

---

## Status

**Milestone 0** — working vertical slice: replay feed → Redis → WebSocket → live chart,
worker persisting to Timescale, chaos suite green.

**Milestone 1** — volatility regime detection end to end: the Schmitt trigger wired into
the ingestor, `alert_events` in TimescaleDB with `UNIQUE (symbol, seq)` idempotency,
`ch:alerts:{SYMBOL}` pushed to the browser, and detector state rehydrated on leader
failover. **148 tests**, `mypy --strict` clean.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the remaining milestones.

## License

MIT
