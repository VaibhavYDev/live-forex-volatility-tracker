import { bench, describe } from "vitest";
import { flushFrames } from "../../../test/setup";
import { MarketStore } from "../store";
import type { Quote } from "../types";

/**
 * The README claims 50 msg/sec renders smoothly. This measures the half of that
 * claim which is ours: how long the store takes to absorb a second of feed and
 * hand React one consistent view of it. Same standard as the backend benchmarks
 * in docs/benchmarks.md — publish the number or delete the adjective.
 *
 * The frame budget is 16.7ms and the store is only one tenant of it; anything
 * here above ~1ms means the chart is competing with bookkeeping.
 *
 * Stores and subscribers are built once, outside the timed body. Constructing
 * them per iteration measured allocation and GC rather than the hot path, which
 * is the mistake that makes most JS microbenchmarks unfalsifiable.
 */

const SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"];

const quote = (symbol: string, i: number): Quote => ({
  symbol,
  bid: 1.09 + i * 1e-6,
  ask: 1.09 + i * 1e-6 + 5e-5,
  mid: 1.09 + i * 1e-6,
  ts: "2026-08-22T12:00:00Z",
});

// Pre-built so the timed body allocates nothing the real hot path would not.
const TICKS = Array.from({ length: 500 }, (_, i) => quote(SYMBOLS[i % SYMBOLS.length]!, i));

function seeded(subscribersPerSymbol: number): MarketStore {
  const store = new MarketStore();
  for (let n = 0; n < subscribersPerSymbol; n++) {
    for (const s of SYMBOLS) store.subQuote(s, () => {});
  }
  return store;
}

const thin = seeded(1);
const wide = seeded(20);
const conflating = seeded(1);

describe("one second of feed", () => {
  bench("50 ticks across 5 pairs, 1 subscriber each", () => {
    for (let i = 0; i < 50; i++) thin.offer(TICKS[i]!);
    flushFrames();
  });

  // The realistic shape under load: a burst lands between two frames, so most of
  // it is conflated away before anything is notified. This is the number that
  // should barely move as the feed gets faster.
  bench("500 ticks conflated into a single frame", () => {
    for (let i = 0; i < 500; i++) conflating.offer(TICKS[i]!);
    flushFrames();
  });

  // Every mounted cell, badge and readout is a subscriber. Notification cost has
  // to stay linear in *interested* subscribers, not in all of them.
  bench("50 ticks with 20 subscribers per pair", () => {
    for (let i = 0; i < 50; i++) wide.offer(TICKS[i]!);
    flushFrames();
  });
});
