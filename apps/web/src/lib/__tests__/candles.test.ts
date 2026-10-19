import { describe, expect, it } from "vitest";
import parity from "../../../../../tests/fixtures/candle_parity.json";
import {
  DAY_S,
  TF_SECONDS,
  bucketOf,
  bucketStart,
  dailySeries,
  dayCandle,
  extend,
  foldMinutes,
  join,
  lowerBound,
  sameCandle,
} from "../candles";
import { MinuteBook } from "../stream/minutes";
import type { Bar } from "../stream/types";

/**
 * Candles from minutes. A wrong fold draws a chart that looks right and is
 * wrong about every candle on it, so each rule is checked three ways: against
 * the server's own folds of the same minutes, against an aggregation written
 * here independently, and against hand-worked cases for the edges.
 */

type Row = [number, number, number, number, number, number, string];
const toBar = ([t, o, h, l, c, n, src]: Row): Bar => ({ t, o, h, l, c, n, src });
const toRow = (b: Bar): Row => [b.t, b.o, b.h, b.l, b.c, b.n, b.src];

const SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"] as const;
const INTRADAY = ["1m", "3m", "5m", "10m", "15m", "30m"] as const;
const utc = (spec: string) => Date.parse(`${spec}Z`) / 1000;

/** Deterministic pseudo-random numbers (mulberry32): the same minutes every run. */
function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4_294_967_296;
  };
}

/**
 * A session of raw cache members for one pair: provider history turning live,
 * one minute reported by both, minutes nothing arrived for, unordered.
 */
function session(symbol: string, from: number, minutes: number): Bar[] {
  const random = rng([...symbol].reduce((h, ch) => h * 31 + ch.charCodeAt(0), 7));
  let price = symbol.endsWith("JPY") ? 150 : symbol === "USDCHF" ? 0.79 : 1.1;
  const out: Bar[] = [];
  const handover = Math.floor(minutes / 3);
  for (let i = 0; i < minutes; i++) {
    if (random() < 0.1) continue;
    const step = () => (random() - 0.5) * price * 4e-4;
    const o = price;
    const c = o + step();
    const h = Math.max(o, c) + Math.abs(step());
    const l = Math.min(o, c) - Math.abs(step());
    price = c;
    const src = i < handover ? "backfill" : "stream";
    const bar = { t: from + i * 60, o, h, l, c, n: src === "backfill" ? 0 : 1 + Math.floor(random() * 80), src };
    if (i === handover) out.push({ ...bar, o: o * 0.9999, h: h * 1.00002, n: 0, src: "backfill" });
    out.push(bar);
  }
  return out.sort(() => random() - 0.5);
}

/**
 * The server's fold, written again without looking at either implementation:
 * group by bucket, order each group by time with provider history first, then
 * read the candle off it.
 */
function aggregate(raw: readonly Bar[], seconds: number): Bar[] {
  const groups = new Map<number, Bar[]>();
  for (const b of raw) {
    const key = b.t - (b.t % seconds);
    groups.set(key, [...(groups.get(key) ?? []), b]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a - b)
    .map(([t, members]) => {
      const ordered = [...members].sort(
        (a, b) => a.t - b.t || Number(a.src !== "backfill") - Number(b.src !== "backfill"),
      );
      const srcs = new Set(ordered.map((m) => m.src));
      return {
        t,
        o: ordered[0]!.o,
        h: Math.max(...ordered.map((m) => m.h)),
        l: Math.min(...ordered.map((m) => m.l)),
        c: ordered[ordered.length - 1]!.c,
        n: ordered.reduce((sum, m) => sum + m.n, 0),
        src: srcs.size === 1 ? ordered[0]!.src : "mixed",
      };
    });
}

/** What the store holds after receiving these reports, in this order. */
function booked(raw: readonly Bar[]): readonly Bar[] {
  const book = new MinuteBook();
  for (const b of raw) book.put(b);
  return book.sorted();
}

describe("buckets", () => {
  it.each(Object.entries(TF_SECONDS))("%s starts on a UTC multiple of its length", (_, s) => {
    const t = utc("2026-10-21T13:47:31");
    const start = bucketStart(t, s);
    expect(start % s).toBe(0);
    expect(start).toBeLessThanOrEqual(t);
    expect(t - start).toBeLessThan(s);
  });

  it("puts 15 and 30 minute candles on the quarter and half hour, whenever the page loaded", () => {
    expect(bucketStart(utc("2026-10-21T10:07:00"), 900)).toBe(utc("2026-10-21T10:00:00"));
    expect(bucketStart(utc("2026-10-21T10:15:00"), 900)).toBe(utc("2026-10-21T10:15:00"));
    expect(bucketStart(utc("2026-10-21T10:44:59"), 1_800)).toBe(utc("2026-10-21T10:30:00"));
  });

  it("puts a day on UTC midnight", () => {
    expect(bucketStart(utc("2026-10-21T23:59:59"), DAY_S)).toBe(utc("2026-10-21T00:00:00"));
  });
});

describe("parity with the server", () => {
  const fixture = parity as unknown as {
    minutes: Record<string, Row[]>;
    folds: Record<string, Record<string, Row[]>>;
  };

  for (const symbol of Object.keys(fixture.minutes)) {
    it.each(INTRADAY)(`folds ${symbol} %s exactly as fx_core.intervals.fold does`, (tf) => {
      const minutes = booked(fixture.minutes[symbol]!.map(toBar));
      expect(foldMinutes(minutes, TF_SECONDS[tf]).map(toRow)).toEqual(fixture.folds[symbol]![tf]);
    });
  }
});

describe("against an independent aggregation", () => {
  const from = utc("2026-10-21T08:00:00");

  for (const symbol of SYMBOLS) {
    it.each(INTRADAY)(`${symbol} %s`, (tf) => {
      const raw = session(symbol, from, 240);
      expect(foldMinutes(booked(raw), TF_SECONDS[tf])).toEqual(aggregate(raw, TF_SECONDS[tf]));
    });

    it(`${symbol} 1d, today from the minutes alone`, () => {
      const raw = session(symbol, from, 240);
      const { bars } = dailySeries([], booked(raw), from + 240 * 60);
      expect(bars).toEqual(aggregate(raw, DAY_S));
    });
  }

  it("does not depend on the order the minutes arrived in", () => {
    const raw = session("EURUSD", from, 120);
    const reversed = [...raw].reverse();
    expect(foldMinutes(booked(reversed), 900)).toEqual(foldMinutes(booked(raw), 900));
  });

  it("leaves a minute nothing arrived for out, rather than filling it", () => {
    const at = utc("2026-10-21T10:00:00");
    const minutes = [0, 1, 4].map((i) => ({
      t: at + i * 60,
      o: 1 + i,
      h: 1.5 + i,
      l: 0.5 + i,
      c: 1.2 + i,
      n: 3,
      src: "stream",
    }));
    const [candle] = foldMinutes(minutes, 300);
    expect(candle).toEqual({ t: at, o: 1, h: 5.5, l: 0.5, c: 5.2, n: 9, src: "stream" });
  });
});

describe("one minute from two sources", () => {
  const t = utc("2026-10-21T10:03:00");
  const provider: Bar = { t, o: 1.1, h: 1.2, l: 1.05, c: 1.15, n: 0, src: "backfill" };
  const stream: Bar = { t, o: 1.14, h: 1.18, l: 1.01, c: 1.16, n: 4, src: "stream" };

  it("opens with the provider, closes with the stream, keeps both extremes", () => {
    expect(join([stream, provider])).toEqual({ t, o: 1.1, h: 1.2, l: 1.01, c: 1.16, n: 4, src: "mixed" });
  });

  it("does not count the stream's ticks twice when the server's merged copy arrives too", () => {
    const merged = join([provider, stream]);
    expect(join([provider, stream, merged])).toEqual(merged);
  });

  it("hands a single report back untouched", () => {
    expect(join([stream])).toBe(stream);
  });
});

describe("the forming minute", () => {
  const start = utc("2026-10-21T10:00:00");
  const sealed: Bar = { t: start, o: 1.1, h: 1.2, l: 1.0, c: 1.15, n: 10, src: "stream" };
  const forming = (h: number, l: number, c: number): Bar => ({
    t: start + 120,
    o: 1.15,
    h,
    l,
    c,
    n: 3,
    src: "stream",
  });

  it("keeps the candle's open, extends the range only when exceeded, and takes the close", () => {
    expect(extend(sealed, forming(1.19, 1.05, 1.17), start)).toEqual({
      t: start,
      o: 1.1,
      h: 1.2,
      l: 1.0,
      c: 1.17,
      n: 13,
      src: "stream",
    });
    expect(extend(sealed, forming(1.25, 0.95, 0.96), start)).toMatchObject({ h: 1.25, l: 0.95 });
  });

  it("is the whole candle when nothing in its bucket has sealed", () => {
    expect(extend(null, forming(1.19, 1.05, 1.17), start)).toMatchObject({ t: start, o: 1.15, n: 3 });
  });

  it("makes a candle of provider history mixed", () => {
    expect(extend({ ...sealed, src: "backfill" }, forming(1.19, 1.05, 1.17), start).src).toBe("mixed");
  });

  it("is never folded into a bucket from minutes at or after it", () => {
    const minutes = [sealed, { ...sealed, t: start + 120, h: 9 }];
    expect(bucketOf(minutes, start, 300, start + 120)).toEqual(sealed);
    expect(bucketOf(minutes, start + 300, 300)).toBeNull();
  });
});

describe("helpers", () => {
  it("finds the first minute at or after a time", () => {
    const minutes = [60, 120, 240].map((t) => ({ t }) as Bar);
    expect([0, 60, 61, 240, 241].map((t) => lowerBound(minutes, t))).toEqual([0, 0, 1, 2, 3]);
  });

  it("compares what is drawn, not where it came from", () => {
    const a: Bar = { t: 0, o: 1, h: 2, l: 0, c: 1, n: 1, src: "stream" };
    expect(sameCandle(a, { ...a, n: 9, src: "mixed" })).toBe(true);
    expect(sameCandle(a, { ...a, h: 3 })).toBe(false);
    expect(sameCandle(a, undefined)).toBe(false);
  });
});

describe("the daily candle", () => {
  // Wednesday 21 October 2026: the market is open all day.
  const day = utc("2026-10-21T00:00:00");
  const minute = (t: number, src = "stream"): Bar => ({
    t,
    o: 1.1,
    h: 1.1 + (t % 7) * 1e-4,
    l: 1.1 - (t % 5) * 1e-4,
    c: 1.1 + (t % 3) * 1e-5,
    n: src === "stream" ? 4 : 0,
    src,
  });
  const run = (from: number, count: number) =>
    Array.from({ length: count }, (_, i) => minute(from + i * 60));
  const days: Bar[] = [
    { t: day - 2 * DAY_S, o: 1.09, h: 1.1, l: 1.08, c: 1.095, n: 0, src: "backfill" },
    { t: day - DAY_S, o: 1.095, h: 1.11, l: 1.09, c: 1.1, n: 0, src: "backfill" },
  ];

  it("keeps the provider's past days and builds today from the minutes", () => {
    const minutes = run(day, 630);
    const { bars, coverage } = dailySeries(days, minutes, day + 630 * 60);
    expect(bars.slice(0, 2)).toEqual(days);
    expect(bars[2]).toEqual(foldMinutes(minutes, DAY_S)[0]);
    expect(coverage).toEqual({ observed: 630, expected: 630 });
  });

  it("joins the provider's bar for today with the minutes, keeping both ranges", () => {
    const provider: Bar = { t: day, o: 1.0999, h: 1.2, l: 1.0, c: 1.1001, n: 0, src: "backfill" };
    const minutes = run(day + 3_600, 60);
    const { bars } = dailySeries([...days, provider], minutes, day + 7_200);
    const today = bars.at(-1)!;
    expect(today).toMatchObject({ t: day, o: 1.0999, h: 1.2, l: 1.0, src: "mixed" });
    expect(today.c).toBe(minutes.at(-1)!.c);
    expect(today.n).toBe(240);
  });

  it("is partial when a minute the market was open has no bar", () => {
    const minutes = run(day, 630).filter((_, i) => i !== 300);
    expect(dailySeries(days, minutes, day + 630 * 60).coverage).toEqual({
      observed: 629,
      expected: 630,
    });
  });

  it("counts the minute still forming as held", () => {
    const minutes = run(day, 629);
    const forming = minute(day + 629 * 60);
    expect(dailySeries(days, minutes, day + 630 * 60, forming).coverage).toEqual({
      observed: 630,
      expected: 630,
    });
  });

  it("is partial, not complete, when only the provider's bar is held for today", () => {
    const provider: Bar = { t: day, o: 1.1, h: 1.2, l: 1.0, c: 1.15, n: 0, src: "backfill" };
    const { bars, coverage } = dailySeries([...days, provider], [], day + 600);
    expect(bars.at(-1)).toEqual(provider);
    expect(coverage).toEqual({ observed: 0, expected: 10 });
  });

  it("expects nothing on a Saturday and draws no candle for it", () => {
    const saturday = utc("2026-10-24T00:00:00");
    expect(dailySeries(days, [], saturday + 43_200)).toEqual({ bars: days, coverage: null });
  });

  it("expects only the minutes since the Sunday reopen", () => {
    // 18 October 2026: the market reopens 21:00 UTC (17:00 New York, EDT).
    const sunday = utc("2026-10-18T00:00:00");
    const reopen = utc("2026-10-18T21:00:00");
    const minutes = run(reopen, 30);
    expect(dailySeries([], minutes, reopen + 30 * 60).coverage).toEqual({
      observed: 30,
      expected: 30,
    });
    expect(dayCandle([], minutes, sunday)?.t).toBe(sunday);
  });

  it("does not count a closed-market minute as observed", () => {
    // A quote stamped before the reopen is not a minute the day was open.
    const reopen = utc("2026-10-18T21:00:00");
    const minutes = [minute(reopen - 120), ...run(reopen, 10)];
    expect(dailySeries([], minutes, reopen + 600).coverage).toEqual({ observed: 10, expected: 10 });
  });

  it("finds the provider's bar for a day that is not the newest", () => {
    expect(dayCandle(days, [], day - 2 * DAY_S)).toEqual(days[0]);
  });
});
