# Milestone 2 — React & Lightweight Charts

**Scope agreed 2026-08-22:** render-pipeline Tier 2 plus measurement; all four regime
visualisations; accessibility fixed as we build.

> The 50 msg/sec problem was already solved in Milestone 0. This milestone fixes three real
> defects in that solution, and then does the thing that actually matters for a portfolio:
> **makes the algorithm visible.** A reviewer cannot read `hysteresis.py` in the ten minutes
> they will give this project. They can look at one chart.

---

## Part A — the render pipeline

### What already works

`useForexStream.ts` accumulates ticks in a `useRef` Map (mutating a ref schedules no render),
drains at most once per `requestAnimationFrame`, and pushes to Lightweight Charts imperatively
via `series.update()` — bypassing React entirely on the 60fps path. React state is reserved
for structural change.

That is the correct shape. These are its three defects.

### A1. The rAF loop never idles

```ts
const pump = () => { /* ... */ rafRef.current = requestAnimationFrame(pump) }
```

It re-schedules unconditionally, so a dead Sunday market still costs 60 wakeups a second
forever. On a laptop that is measurable battery drain for zero work.

**Fix:** schedule on demand. `offer()` starts a frame if none is pending; `pump` re-schedules
only if the buffer refilled while it was applying.

```ts
private schedule() {
  if (this.frame !== null) return          // already pending — this is the whole trick
  this.frame = requestAnimationFrame(this.pump)
}
private pump = () => {
  this.frame = null
  const batch = this.drain()
  if (batch.length) this.applyBatch(batch)
  if (this.buffer.size > 0) this.schedule()
}
```

Idle market → zero frames scheduled. Verifiable in a test by counting `requestAnimationFrame`
calls with fake timers.

### A2. One `setQuotes` per frame re-renders the entire tree

50/sec → 60/sec was the big win, but a single EURUSD tick currently re-renders `App`, all five
tabs, the chart wrapper and the panel. React is doing reconciliation work proportional to the
whole UI for a change that affects one number.

**Fix:** an external store with **per-symbol** subscriptions, read through
`useSyncExternalStore`. A `<PriceCell symbol="EURUSD" />` leaf then re-renders alone.

```ts
// src/lib/stream/store.ts
class MarketStore {
  private quotes = new Map<string, Quote>()
  private listeners = new Map<string, Set<() => void>>()

  subscribe(symbol: string, cb: () => void): () => void
  getQuote(symbol: string): Quote | undefined
  applyBatch(batch: Quote[]): void   // notifies ONLY the symbols in the batch
}
```

**The trap, stated up front because it is the standard way to get this wrong:**
`getSnapshot` must be **referentially stable** — return the identical object until the value
actually changes, or React re-renders forever and the dev-mode "getSnapshot should be cached"
warning fires. Storing frozen `Quote` objects and replacing the reference on update gives that
for free. A test asserts `getQuote()` returns the same reference across two no-op frames.

### A3. Hidden series are still updated

We call `series.update()` for charts nobody is looking at.

**Fix:** split the two concerns. *Every* symbol updates the store (a Map write — free, and the
tab prices must stay live). Only the **active** symbol's imperative chart callback fires.
`store.setActiveSymbol()` when the tab changes.

### A4. Measurement

Same standard as the backend: numbers, not adjectives.

- `PerformanceObserver({ entryTypes: ['longtask'] })` — count and total duration of tasks
  blocking the main thread >50ms. The target is zero under sustained load.
- A frame-budget histogram: time from `pump()` entry to exit, p50/p99.
- Dropped-frame count from `requestAnimationFrame` timestamp deltas.

Published in `docs/benchmarks.md` next to the server-side figures, driven by the replay
provider at `speed=100`.

**Explicitly out of scope:** the Tier-3 Web Worker (socket + parse + conflate off the main
thread). It is the right answer past ~20 symbols and the wrong thing to build before Tier 2 is
measured. Recorded here so the decision is visible rather than forgotten.

---

## Part B — making the regime visible

Four layers, in value order. Layer 1 alone justifies the milestone.

### B1. The z-score pane — the one that explains the algorithm

A second, time-synced Lightweight Charts instance below the price chart:

* a line series of `z` over time;
* dashed price lines at `enter_z = 3.0` and `exit_z = 1.5`;
* **the hysteresis band shaded between them.**

You can then *watch* the mechanism: the signal crosses 3.0 and escalates, wanders around 2.2
inside the band without clearing, and only clears when it finally drops through 1.5. Every
paragraph written about Schmitt triggers in this repo becomes one picture.

**Implementation note.** Lightweight Charts has no native horizontal band. `createPriceLine`
draws the two thresholds; for the fill, read `priceToCoordinate(3.0)` and
`priceToCoordinate(1.5)` and position an absolute div over the canvas, re-measuring on
`subscribeVisibleLogicalRangeChange` and on resize. Simpler and more robust than a custom
series plugin, and it survives library upgrades.

**Time-sync** both panes by mirroring `timeScale().subscribeVisibleLogicalRangeChange` in each
direction, with a re-entrancy guard so they do not ping-pong.

**⚠ This needs a data source we do not have.** `vol:{SYMBOL}:1h` stores the *current* z, not a
series, and `/api/volatility/...` returns no history. Two options:

| | cost | result |
|---|---|---|
| Accumulate client-side from `vol` frames | zero backend change | pane is **empty on page load**, fills as you watch |
| `ZADD zhist:{SYMBOL}` in the ingestor, TTL'd, served in the subscribe snapshot | ~10 lines in `pipeline.py` + snapshot read | pane is populated immediately |

**Recommend the second.** An explanatory chart that is blank when a reviewer first opens the
page explains nothing. It reuses the exact pattern already used for `hist:{SYMBOL}:1m`.

### B2. Transition markers on the price chart

`series.setMarkers()` — arrow up at escalation, down at clear, labelled with the `cause`
(`threshold` / `baseline_thaw` / `observation_lost`, which mean different things and should
read differently). Discrete events rendered as discrete marks, matching the event model.

`setMarkers` replaces the whole array rather than appending, so hold the list in state and
re-set on change. Free at fewer than fifty markers.

### B3. Regime timeline strip

A thin state bar under the chart — plain SVG, not a chart — coloured and hatched by regime
over time, sharing the chart's visible time range. Scannable across all pairs at once, which
is what someone watching a watchlist actually wants.

### B4. Background shading on stressed ranges

Same full-height histogram trick already used for the `source='backfill'` band in
`PriceChart.tsx`. Cheapest of the four; do it last.

---

## Part C — accessibility, fixed as we build

**Two WCAG failures exist in the code today.** The regime badge (green vs amber) and the `hot`
tab (border colour only) convey state by **colour alone** — WCAG 1.4.1. Every one of the four
visualisations above would add another instance if we build them the same way.

* **Three channels for regime, always:** colour + shape/icon + text label. `STRESSED` with a
  filled triangle reads correctly in greyscale, at 8pt, and to a colour-blind reviewer.
* **Charts are canvas, and therefore invisible to a screen reader.** Each chart gets a
  visually-hidden live summary — current price, current σ, current regime, last transition.
  This is the accessibility failure that every dashboard project ships with.
* **Toasts:** `role="alert"` for escalation, `role="status"` for clear; no auto-dismiss under
  20 s (WCAG 2.2.1) or provide a pause control; slide-in suppressed under
  `prefers-reduced-motion`.
* **Tabs:** a real `role="tablist"` with roving tabindex and arrow-key navigation, not five
  buttons.
* **Contrast:** verify the amber `--warn` (#d69e2e) against the panel background — it is
  borderline for small text on the dark theme and may need lightening.

---

## Part D — the frontend has no tests at all

That is the largest gap in the repo right now, and it undercuts the "every claim has a test"
standard the backend holds itself to.

```
apps/web/
├── vitest.config.ts
└── src/**/__tests__/
    ├── store.test.ts          conflation, per-symbol notification, idle rAF,
    │                          referential stability of getSnapshot
    ├── regime.test.tsx        badge/marker/strip render the right state
    ├── a11y.test.tsx          axe-core: zero violations on each view
    └── perf.bench.ts          frame budget under a synthetic firehose
```

Vitest + Testing Library + `jest-axe`, wired into the existing `web` CI job alongside
`tsc --noEmit` and `npm run build`.

---

## Order of work

| # | Step | Why first |
|---|---|---|
| 1 | Vitest + axe harness | Nothing after this is verifiable without it |
| 2 | `MarketStore` + `useSyncExternalStore` (A1–A3) | Everything else renders through it |
| 3 | `zhist` in the ingestor + snapshot | B1 is blank without it; small and backend-side |
| 4 | z-score pane with hysteresis band (B1) | The milestone's centrepiece |
| 5 | Markers + timeline strip + shading (B2–B4) | Cheap once the data plumbing exists |
| 6 | A11y pass + `docs/benchmarks.md` update | Verification, not decoration |

Steps 2 and 4 are the two with real design risk. Everything else is assembly.
