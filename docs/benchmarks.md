# Benchmarks

Real measurements, reproducible with `make bench` (`uv run pytest tests/load -m load -s`).
Every performance claim in the README traces to a number here. Adjectives without
numbers are marketing.

Environment: 1 vCPU container, Python 3.11, Redis 7, PostgreSQL 16. Absolute values
will differ on your machine; the **ratios** are the point, and they are what the
assertions check.

---

## 1. The volatility hot path is O(1) per tick

Welford + EWMA + bucket accumulation, measured over the first 100k ticks and again
after 900k ticks of accumulated state:

| | ns/tick |
|---|---|
| first 100,000 ticks | 801 |
| after 900,000 ticks | 788 |
| **ratio** | **0.98×** |

A ratio near 1.0 is the whole claim. A naive sliding window that recomputes over its
contents is O(n) per tick, so its cost climbs steadily and the system degrades the
longer it stays up — the worst failure shape there is, because it passes every short
test and only shows up in production after a few hours.

At ~800 ns/tick, one core can sustain roughly **1.2 million ticks/second** of
volatility maths. The upstream feed delivers 10–50/sec. The maths will never be the
bottleneck, which is exactly the point of choosing the O(1) formulation up front.

Asserted by `test_volatility_hot_path_is_o1_per_tick`.

---

## 2. Conflation bounds memory under a firehose

200,000 updates published to one slow client subscribed to 5 symbols:

| | |
|---|---|
| updates published | 200,000 |
| publish throughput | **1,506k/s** |
| entries held in memory | **5** (= subscribed symbols) |
| updates conflated | 199,995 |
| a naive unbounded queue would hold | 200,000 |

Memory is bounded by *subscribed symbols*, not by message rate — which is the
difference between a server that survives a slow client and one that gets OOM-killed
by it. The 199,995 superseded updates were all stale prices nobody would have wanted
to see.

Asserted by `test_conflation_bounds_memory_under_a_firehose`.

---

## 3. Broadcast stays fast with 500 clients

Fan-out of a single tick to 500 connected sessions, where 450 of them never read:

| | µs |
|---|---|
| p50 | 312 |
| p99 | **533** |

A p99 of 0.5 ms across 500 clients means no client is blocking the loop. In the naive
`await ws.send_json()` design this figure is unbounded — it is whatever the slowest
client's network decides, and every other client waits for it.

Slow clients still held only 1 queued entry each throughout.

Asserted by `test_broadcast_latency_across_many_clients`.

---

## 4. Replay provider throughput

| | ticks/s |
|---|---|
| target | 2,000 |
| achieved | 840 |

Honest note: the shortfall is `asyncio.sleep()` granularity, not the pipeline — the
event loop cannot reliably sleep for 500 µs. For load tests that need a genuine
firehose, use `speed=100` (which scales the simulated clock) rather than raising
`ticks_per_sec`, or drop the inter-tick sleep entirely in a burst-mode fixture.

Stating this rather than quietly reporting "2,000/s" matters: a benchmark you cannot
reproduce is worse than no benchmark.

Asserted by `test_replay_provider_sustains_target_rate`.

---

## What is not benchmarked yet

- **End-to-end p99 latency, provider timestamp → browser paint.** The histogram
  (`fx_ingest_lag_seconds`) is wired and exported; the browser half needs a
  `performance.mark()` round trip to close the loop. This is the headline number and
  it is not honest to publish it before measuring it properly.
- **Sustained multi-hour soak.** The O(1) result predicts flat memory and CPU; that
  prediction is untested beyond a million ticks.
- **Postgres write throughput under a real firehose.** The batched upsert is
  correct (verified against PostgreSQL 16) but its ceiling is unmeasured.

Listing the gaps is part of the benchmark. A performance section that only reports
wins is a sales document.
