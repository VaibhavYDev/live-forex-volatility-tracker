# 0003 — Welford's algorithm for streaming variance

**Status:** Accepted · **Date:** 2026-08-22

## Context

We need running variance over a tick stream, updated per tick, without storing
history.

## Decision

Welford's online algorithm, with Chan et al.'s parallel `merge` so windows can be
composed from per-minute accumulators.

## Why not the textbook one-liner

`(Σx² − (Σx)²/n) / (n−1)` is one line and O(1). It is also catastrophically wrong
for this data. FX prices sit at ~1.08 with variation of ~1e-5, so the formula
subtracts two nearly equal large numbers and loses most of the significant digits.

Measured on 200,000 simulated EURUSD ticks (σ ≈ 1e-5 around 1.0842):

| method | relative error vs. exact |
|---|---|
| Welford | **1.4e-12** |
| naive sum-of-squares | 8.0e-4 |

Nine orders of magnitude, from four lines of code. The test asserting this is in
`tests/unit/test_volatility.py::test_beats_naive_sum_of_squares_on_fx_scale_data`.

## Consequences

Welford cannot cheaply *remove* an observation — the subtraction form is unstable
and forfeits the guarantee we adopted it for. So sliding windows are built from
additive per-minute sufficient statistics instead (`fx_core/volatility/buckets.py`),
combined via `merge`. A 1h window is 60 merges regardless of whether 600 or 600,000
ticks arrived.

We do persist `sum_ret` / `sum_ret_sq` rather than `m2`, because a SQL `SUM()` can
produce them and that keeps the rollup a single query. `Welford.from_sums` rehydrates
from them and is measurably lossier (~1e-15 relative) than carrying `m2` — still six
orders of magnitude better than the naive formula, and worth the trade for making
`bars_5m` / `bars_1h` pure addition.
