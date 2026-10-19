import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushFrames } from "../../../test/setup";
import { MarketStore } from "../store";
import type { Bar } from "../types";

/**
 * The store's minutes: what the chart folds every timeframe from. The forming
 * minute arrives on every tick frame, so its rules have to hold for frames that
 * repeat, arrive late, race the snapshot or skip a minute - and it must never
 * cost a React render on its own.
 */

const T = 1_792_598_400; // 2026-10-21 16:00 UTC
const S = "EURUSD";
const iso = (sec: number) => new Date((T + sec) * 1000).toISOString();
const bar = (over: Partial<Bar> = {}): Bar => ({
  t: T,
  o: 1.1,
  h: 1.2,
  l: 1.0,
  c: 1.15,
  n: 10,
  src: "stream",
  ...over,
});

let store: MarketStore;
let renders: ReturnType<typeof vi.fn>;
beforeEach(() => {
  store = new MarketStore();
  renders = vi.fn();
  store.subMinutes(S, renders);
});

describe("the forming minute", () => {
  it("is replaced by each newer report of the same minute without a render", () => {
    store.applyForming(S, bar({ n: 1, h: 1.15, c: 1.15 }));
    store.applyForming(S, bar({ n: 2 }));
    expect(store.forming(S)).toEqual(bar({ n: 2 }));
    expect(store.minutes(S)).toEqual([]);
    expect(renders).not.toHaveBeenCalled();
  });

  it("ignores a report with fewer ticks than it holds: an older frame", () => {
    store.applyForming(S, bar({ n: 9 }));
    store.applyForming(S, bar({ n: 4, h: 9 }));
    expect(store.forming(S)).toEqual(bar({ n: 9 }));
  });

  it("ignores a report of an earlier minute", () => {
    store.applyForming(S, bar({ t: T + 60 }));
    store.applyForming(S, bar());
    expect(store.forming(S)!.t).toBe(T + 60);
    expect(store.minutes(S)).toEqual([]);
  });

  it("ignores a report of a minute that has already sealed", () => {
    store.sealMinute(S, bar());
    store.applyForming(S, bar({ n: 99, h: 9 }));
    expect(store.forming(S)).toBeUndefined();
    expect(store.minutes(S)).toEqual([bar()]);
  });

  it("holds the minute that ended until its seal, so its range never shrinks", () => {
    store.applyForming(S, bar({ h: 1.3 }));
    store.applyForming(S, bar({ t: T + 60, n: 1 }));
    expect(renders).toHaveBeenCalledTimes(1);
    expect(store.minutes(S)).toEqual([bar({ h: 1.3 })]);

    store.sealMinute(S, bar({ h: 1.31, n: 11 }));
    expect(store.minutes(S)).toEqual([bar({ h: 1.31, n: 11 })]);
    expect(store.forming(S)!.t).toBe(T + 60);
  });

  it("applies the minute on arrival, so per-frame conflation cannot lose it", () => {
    // Two frames inside one animation frame: the price buffer keeps the
    // second, the minute has already taken the first.
    store.applyForming(S, bar({ h: 1.3 }));
    store.offer({ symbol: S, bid: 1.1, ask: 1.1, mid: 1.1, ts: iso(1) });
    store.applyForming(S, bar({ t: T + 60, n: 1 }));
    store.offer({ symbol: S, bid: 1.1, ask: 1.1, mid: 1.1, ts: iso(61) });
    flushFrames();
    expect(store.stats.conflated).toBe(1);
    expect(store.minutes(S)[0]!.h).toBe(1.3);
  });
});

describe("a sealed minute", () => {
  it("ends the forming minute it seals", () => {
    store.applyForming(S, bar());
    store.sealMinute(S, bar({ n: 12 }));
    expect(store.forming(S)).toBeUndefined();
    expect(store.minutes(S)).toEqual([bar({ n: 12 })]);
  });

  it("keeps what was seen of a minute whose frames were missed", () => {
    store.applyForming(S, bar());
    store.sealMinute(S, bar({ t: T + 60 }));
    expect(store.minutes(S).map((b) => b.t)).toEqual([T, T + 60]);
    expect(store.forming(S)).toBeUndefined();
  });

  it("does not render again for a seal it already holds", () => {
    store.sealMinute(S, bar());
    store.sealMinute(S, bar());
    expect(renders).toHaveBeenCalledTimes(1);
  });
});

describe("snapshot and history", () => {
  it("merges the snapshot's minutes and drops a forming minute it shows sealed", () => {
    store.applyForming(S, bar({ t: T + 60 }));
    store.mergeMinutes(S, [bar(), bar({ t: T + 60, n: 30 })]);
    expect(store.minutes(S).map((b) => b.t)).toEqual([T, T + 60]);
    expect(store.forming(S)).toBeUndefined();
  });

  it("drops a minute the server never sealed once a snapshot covers it", () => {
    // Forming at T when the ingestor restarted; the next minute it sealed was
    // T+120. The browser had promoted T provisionally; the server never will.
    store.applyForming(S, bar());
    store.applyForming(S, bar({ t: T + 60, n: 1 }));
    store.sealMinute(S, bar({ t: T + 60 }));
    store.mergeMinutes(S, [bar({ t: T - 60 }), bar({ t: T + 60 })]);
    expect(store.minutes(S).map((b) => b.t)).toEqual([T - 60, T + 60]);
  });

  it("needs the history request once per pair, then not again", () => {
    expect(store.needsHistory(S)).toBe(true);
    store.mergeMinutes(S, [bar()], "history");
    store.historyLoaded(S, store.minuteGen(S));
    expect(store.needsHistory(S)).toBe(false);
    expect(store.needsHistory("GBPUSD")).toBe(true);
  });

  it("needs it again after a reconnect whose snapshot leaves a hole", () => {
    store.mergeMinutes(S, [bar()], "history");
    store.historyLoaded(S, store.minuteGen(S));

    // Overlapping the minutes held: contiguous, nothing to fetch.
    store.mergeMinutes(S, [bar(), bar({ t: T + 60 })]);
    expect(store.needsHistory(S)).toBe(false);

    // Starting after the newest minute held: the tab was away too long.
    store.mergeMinutes(S, [bar({ t: T + 600 })]);
    expect(store.needsHistory(S)).toBe(true);
  });

  it("keeps the minutes reference stable between changes", () => {
    store.mergeMinutes(S, [bar()]);
    expect(store.minutes(S)).toBe(store.minutes(S));
    expect(store.minutes("GBPUSD")).toBe(store.minutes("USDJPY"));
  });
});

describe("a server that predates the forming bar", () => {
  it("builds the minute from the prices that arrive, claiming no ticks", () => {
    store.applyQuote(S, 1.1, iso(5));
    store.applyQuote(S, 1.2, iso(20));
    store.applyQuote(S, 1.15, iso(40));
    expect(store.forming(S)).toEqual({ t: T, o: 1.1, h: 1.2, l: 1.1, c: 1.15, n: 0, src: "stream" });

    store.applyQuote(S, 1.16, iso(62));
    expect(store.minutes(S)).toEqual([{ t: T, o: 1.1, h: 1.2, l: 1.1, c: 1.15, n: 0, src: "stream" }]);
  });
});

describe("daily series and resets", () => {
  it("stores the daily series with when it was fetched", () => {
    const days = [bar({ t: T - 86_400, src: "backfill" })];
    store.setDaily(S, days, 1234);
    expect(store.daily(S)).toEqual(days);
    expect(store.dailyAt(S)).toBe(1234);
    expect(store.dailyAt("GBPUSD")).toBe(0);
  });

  it("forgets another provider's minutes and asks for history again", () => {
    store.mergeMinutes(S, [bar()], "history");
    store.historyLoaded(S, store.minuteGen(S));
    store.applyForming(S, bar({ t: T + 60 }));
    store.setDaily(S, [bar()]);

    store.clearMarket();
    expect(store.minutes(S)).toEqual([]);
    expect(store.forming(S)).toBeUndefined();
    expect(store.daily(S)).toEqual([]);
    expect(store.needsHistory(S)).toBe(true);
  });

  it("drops everything on reset", () => {
    store.mergeMinutes(S, [bar()]);
    store.reset();
    expect(store.minutes(S)).toEqual([]);
    expect(store.minuteGen(S)).toBe(0);
  });
});
