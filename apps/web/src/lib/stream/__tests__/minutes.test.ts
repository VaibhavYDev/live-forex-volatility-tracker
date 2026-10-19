import { describe, expect, it } from "vitest";
import { MINUTE_CAP, MinuteBook } from "../minutes";
import type { Bar } from "../types";

/**
 * The same minute reaches the browser up to four ways - snapshot, history
 * request, sealed frame, last forming frame - in any order and more than once.
 * Whatever the order, the minute shown must be the same, and a repeat must not
 * count anything twice.
 */

const T = 1_792_598_400; // a minute boundary
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

describe("the minute book", () => {
  it("ignores a report it already holds, keeping the same reference", () => {
    const book = new MinuteBook();
    expect(book.put(bar())).toBe(true);
    const before = book.sorted();
    expect(book.put(bar())).toBe(false);
    expect(book.sorted()).toBe(before);
  });

  it("replaces a source's earlier report of a minute with its later one", () => {
    const book = new MinuteBook();
    book.put(bar({ c: 1.11 }));
    book.put(bar({ c: 1.12 }));
    expect(book.sorted()).toEqual([bar({ c: 1.12 })]);
  });

  it("joins provider history and the stream for the same minute", () => {
    const book = new MinuteBook();
    book.put(bar({ o: 1.09, h: 1.25, c: 1.13, n: 0, src: "backfill" }));
    book.put(bar());
    expect(book.sorted()).toEqual([bar({ o: 1.09, h: 1.25, src: "mixed" })]);
  });

  it("does not double the tick count when the server's merged copy arrives too", () => {
    const book = new MinuteBook();
    book.put(bar({ o: 1.09, n: 0, src: "backfill" }));
    book.put(bar());
    const merged = book.sorted()[0]!;
    expect(book.put(merged)).toBe(false);
    expect(book.sorted()[0]!.n).toBe(10);
  });

  it("holds the minutes in time order, one per minute, however they arrived", () => {
    const book = new MinuteBook();
    for (const k of [3, 1, 2, 1, 0]) book.put(bar({ t: T + k * 60 }));
    expect(book.sorted().map((b) => b.t)).toEqual([T, T + 60, T + 120, T + 180]);
    expect(Object.isFrozen(book.sorted())).toBe(true);
    expect(book.newest).toBe(T + 180);
  });

  describe("a provisional minute", () => {
    it("is shown until its seal arrives, then replaced by it", () => {
      const book = new MinuteBook();
      book.provisional(bar({ h: 1.3, n: 12 }));
      expect(book.sealed(T)).toBe(false);
      expect(book.sorted()[0]).toMatchObject({ h: 1.3, src: "stream" });

      // The seal has every tick; it wins even where it is narrower - an
      // ingestor that restarted mid-minute starts the minute again.
      book.put(bar({ h: 1.2, n: 7 }));
      expect(book.sealed(T)).toBe(true);
      expect(book.sorted()).toEqual([bar({ h: 1.2, n: 7 })]);
    });

    it("never replaces a sealed minute", () => {
      const book = new MinuteBook();
      book.put(bar());
      expect(book.provisional(bar({ h: 9 }))).toBe(false);
      expect(book.sorted()).toEqual([bar()]);
    });

    it("sits beside provider history for the same minute until sealed", () => {
      const book = new MinuteBook();
      book.put(bar({ o: 1.09, n: 0, src: "backfill" }));
      book.provisional(bar({ l: 0.9 }));
      expect(book.sorted()[0]).toMatchObject({ o: 1.09, l: 0.9, src: "mixed" });
      expect(book.sealed(T)).toBe(false);
    });

    it("is superseded by the server's merged copy as well", () => {
      const book = new MinuteBook();
      book.provisional(bar({ h: 1.3 }));
      book.put(bar({ src: "mixed" }));
      expect(book.sorted()).toEqual([bar({ src: "mixed" })]);
    });
  });

  describe("a minute that never sealed", () => {
    it("is dropped when an authoritative range shows the server never had it", () => {
      // The ingestor restarted while T+60 was forming: the browser held what
      // it saw of it, the server never sealed it.
      const book = new MinuteBook();
      book.put(bar());
      book.provisional(bar({ t: T + 60 }));
      book.put(bar({ t: T + 120 }));
      expect(book.dropUnsealed(T, T + 120, new Set([T, T + 120]))).toBe(true);
      expect(book.sorted().map((b) => b.t)).toEqual([T, T + 120]);
    });

    it("is kept when it lies beyond the range, still awaiting its seal", () => {
      const book = new MinuteBook();
      book.put(bar());
      book.provisional(bar({ t: T + 60 }));
      expect(book.dropUnsealed(T, T, new Set([T]))).toBe(false);
      expect(book.sorted()).toHaveLength(2);
    });

    it("leaves provider history for the minute in place", () => {
      const book = new MinuteBook();
      book.put(bar({ n: 0, src: "backfill" }));
      book.provisional(bar({ h: 1.3 }));
      book.dropUnsealed(T, T, new Set());
      expect(book.sorted()).toEqual([bar({ n: 0, src: "backfill" })]);
    });

    it("never drops a sealed minute", () => {
      const book = new MinuteBook();
      book.put(bar());
      expect(book.dropUnsealed(T, T, new Set())).toBe(false);
      expect(book.sorted()).toEqual([bar()]);
    });
  });

  it("keeps a bounded number of minutes, dropping the oldest in batches", () => {
    const book = new MinuteBook();
    const total = MINUTE_CAP + 61;
    for (let i = 0; i < total; i++) book.put(bar({ t: T + i * 60 }));
    expect(book.size).toBe(MINUTE_CAP);
    expect(book.sorted()[0]!.t).toBe(T + (total - MINUTE_CAP) * 60);
    expect(book.newest).toBe(T + (total - 1) * 60);
  });

  it("forgets everything on clear", () => {
    const book = new MinuteBook();
    book.put(bar());
    book.forming = bar({ t: T + 60 });
    book.clear();
    expect(book.sorted()).toEqual([]);
    expect(book.forming).toBeUndefined();
    expect(book.newest).toBeUndefined();
  });
});
