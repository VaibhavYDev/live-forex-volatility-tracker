import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as lwc from "../../test/lwc";

vi.mock("lightweight-charts", () => ({ createChart: lwc.createChart }));

import { flushFrames } from "../../test/setup";
import { TF_SECONDS, bucketStart, foldMinutes } from "../../lib/candles";
import { StoreCtx } from "../../lib/stream/hooks";
import { MarketStore } from "../../lib/stream/store";
import type { Bar, Transition } from "../../lib/stream/types";
import { ThemeProvider } from "../../lib/theme";
import { PriceChart } from "../charts/PriceChart";
import { TIMEFRAMES } from "../TimeframeBar";

/**
 * The chart, with Lightweight Charts replaced by a recorder. What is asserted
 * is what the chart hands the library: which candles, through `setData` or
 * `update`, in what order - the pixels are the library's business.
 */

const BASE = 1_792_576_800; // Wednesday 2026-10-21 10:00 UTC
const LABEL: Record<string, string> = {
  "1m": "1 minute",
  "3m": "3 minutes",
  "5m": "5 minutes",
  "10m": "10 minutes",
  "15m": "15 minutes",
  "30m": "30 minutes",
  "1d": "1 day",
};

let store: MarketStore;

beforeEach(() => {
  store = new MarketStore();
  lwc.reset();
  // History requests stay pending unless a test answers them.
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => {})),
  );
});

// Not unstubAllGlobals: that would also take away the setup's rAF and
// ResizeObserver stand-ins. Each test stubs fetch afresh above.
afterEach(() => {
  vi.useRealTimers();
});

/** Minute `i` after 10:00, drifting up so every candle has a distinct shape. */
const m = (i: number, over: Partial<Bar> = {}): Bar => ({
  t: BASE + i * 60,
  o: 1.08 + i * 1e-4,
  h: 1.08 + i * 1e-4 + 5e-4,
  l: 1.08 + i * 1e-4 - 5e-4,
  c: 1.08 + (i + 1) * 1e-4,
  n: 10,
  src: "stream",
  ...over,
});
const minutes = (count: number, from = 0, over: Partial<Bar> = {}) =>
  Array.from({ length: count }, (_, i) => m(from + i, over));

/** Minutes the history request already delivered, so nothing is fetched. */
function loaded(symbol: string, bars: Bar[]) {
  act(() => {
    store.mergeMinutes(symbol, bars, "history");
    store.historyLoaded(symbol, store.minuteGen(symbol));
  });
}

/** One tick frame: the minute so far, and the price that rides with it. */
function live(symbol: string, bar: Bar) {
  act(() => {
    store.applyForming(symbol, bar);
    store.offer({
      symbol,
      bid: bar.c,
      ask: bar.c,
      mid: bar.c,
      ts: new Date((bar.t + 30) * 1000).toISOString(),
    });
    flushFrames();
  });
}

const ui = (symbol: string) => (
  <ThemeProvider>
    <StoreCtx.Provider value={store}>
      <PriceChart symbol={symbol} />
    </StoreCtx.Provider>
  </ThemeProvider>
);
const mount = (symbol: string) => render(ui(symbol));
const pick = (tf: string) => act(() => screen.getByRole("button", { name: LABEL[tf] }).click());

type Point = { time: number; open: number; high: number; low: number; close: number; color?: string };
const updates = (): Point[] => lwc.chart!.candles.update.mock.calls.map((c) => c[0] as Point);
const drawn = (): Point[] => lwc.chart!.candles.setData.mock.calls.at(-1)![0] as Point[];
const candle = (b: Bar) => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c });
/** The store behind the chart, for the tests that drive it directly. */
const fx = () => ({ store });
/** How often the chart was fitted or zoomed by code, rather than by a user. */
const zoom = () => [
  lwc.timeScale.fitContent.mock.calls.length,
  lwc.timeScale.setVisibleLogicalRange.mock.calls.length,
  lwc.timeScale.applyOptions.mock.calls.length,
];

describe("the live candle on every timeframe", () => {
  it.each(TIMEFRAMES.filter((tf) => tf !== "1d"))(
    "%s: its bucket's sealed minutes, extended by the minute forming",
    (tf) => {
      // 10:00-10:36 sealed, 10:37 forming: inside a bucket on every timeframe.
      const sealed = minutes(37);
      loaded("EURUSD", sealed);
      mount("EURUSD");
      pick(tf);

      const forming = m(37, { h: 2, l: 0.5, c: 1.5, n: 3 });
      live("EURUSD", forming);

      const seconds = TF_SECONDS[tf];
      const start = bucketStart(forming.t, seconds);
      const inBucket = sealed.filter((b) => b.t >= start);
      const got = updates().at(-1)!;
      expect(got).toMatchObject({
        time: start,
        open: inBucket[0]?.o ?? forming.o,
        high: 2,
        low: 0.5,
        close: 1.5,
      });
      // Nothing was fetched to switch, or to move.
      expect(fetch).not.toHaveBeenCalled();
    },
  );

  it("1d: the provider's day joined with today's minutes, extended by the minute forming", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime((BASE + 38 * 60) * 1000);
    const today = bucketStart(BASE, 86_400);
    const day: Bar = { t: today, o: 1.07, h: 1.2, l: 1.0, c: 1.08, n: 0, src: "backfill" };
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => ({ bars: [day] }),
    } as Response);
    loaded("EURUSD", minutes(37));
    mount("EURUSD");
    pick("1d");
    await waitFor(() => expect(store.daily("EURUSD")).toHaveLength(1));

    live("EURUSD", m(37, { h: 1.3, c: 1.09 }));
    expect(updates().at(-1)).toMatchObject({ time: today, open: 1.07, high: 1.3, low: 1.0, close: 1.09 });
  });

  it("keeps the open, extends the range only when exceeded, and moves the close", () => {
    loaded("EURUSD", minutes(5));
    mount("EURUSD");
    pick("15m");
    const open = m(0).o;

    // Inside the range so far: only the close moves.
    live("EURUSD", m(5, { h: 1.0806, l: 1.0799, c: 1.08, n: 1 }));
    live("EURUSD", m(5, { h: 1.0806, l: 1.0799, c: 1.0801, n: 2 }));
    expect(updates().at(-1)).toMatchObject({ open, high: m(4).h, low: m(0).l, close: 1.0801 });

    live("EURUSD", m(5, { h: 1.09, l: 1.07, c: 1.075, n: 3 }));
    expect(updates().at(-1)).toMatchObject({ open, high: 1.09, low: 1.07, close: 1.075 });
  });

  it("loses no extreme to frame conflation", () => {
    // Two frames inside one painted frame: the price of the first is gone,
    // its high is not, because the minute is cumulative.
    loaded("EURUSD", minutes(5));
    mount("EURUSD");
    pick("5m");
    act(() => {
      store.applyForming("EURUSD", m(5, { h: 1.2, c: 1.19, n: 1 }));
      store.offer({ symbol: "EURUSD", bid: 1.19, ask: 1.19, mid: 1.19, ts: new Date().toISOString() });
      store.applyForming("EURUSD", m(5, { h: 1.2, c: 1.09, n: 2 }));
      store.offer({ symbol: "EURUSD", bid: 1.09, ask: 1.09, mid: 1.09, ts: new Date().toISOString() });
      flushFrames();
    });
    expect(updates().at(-1)).toMatchObject({ time: BASE + 300, high: 1.2, close: 1.09 });
  });

  it("starts a new candle on the UTC boundary and settles the one before", () => {
    loaded("EURUSD", minutes(14));
    mount("EURUSD");
    pick("15m");
    live("EURUSD", m(14));
    live("EURUSD", m(15, { n: 1 }));

    const settled = updates().filter((p) => p.time === BASE).at(-1)!;
    expect(settled).toMatchObject(candle(foldMinutes(minutes(15), 900)[0]!));
    expect(settled.color).toBeUndefined();
    expect(updates().at(-1)).toMatchObject({ time: BASE + 900, open: m(15).o });
  });

  it("never redraws a candle once a newer one has started", () => {
    loaded("EURUSD", minutes(14));
    mount("EURUSD");
    pick("15m");
    live("EURUSD", m(14));
    live("EURUSD", m(15, { n: 1 }));
    lwc.chart!.candles.update.mockClear();

    // A late frame for the earlier candle's last minute: the live candle is
    // repainted as it was, the finished one is not touched.
    live("EURUSD", m(14, { n: 99, h: 9 }));
    expect(updates().map((p) => p.time)).not.toContain(BASE);
    expect(updates().every((p) => p.high !== 9)).toBe(true);
  });

  it("only ever moves forward", () => {
    loaded("EURUSD", minutes(20));
    mount("EURUSD");
    pick("3m");
    for (let i = 20; i < 40; i++) {
      live("EURUSD", m(i, { n: 1 }));
      live("EURUSD", m(i, { n: 2 }));
      act(() => store.sealMinute("EURUSD", m(i, { n: 2 })));
    }
    const times = updates().map((p) => p.time);
    expect(times).toEqual([...times].sort((a, b) => a - b));
  });

  it("opens mid-candle with the candle's whole range so far", () => {
    // A browser that connects at 10:37 into a 30-minute candle begun at 10:30.
    act(() => store.mergeMinutes("EURUSD", [...minutes(30), m(30, { h: 1.5 }), ...minutes(6, 31)]));
    act(() => store.historyLoaded("EURUSD", store.minuteGen("EURUSD")));
    act(() => store.applyForming("EURUSD", m(37, { n: 2 })));
    mount("EURUSD");
    pick("30m");
    expect(updates().at(-1)).toMatchObject({ time: BASE + 1_800, open: m(30).o, high: 1.5 });
  });

  it("draws nothing that was not observed", () => {
    // A weekend: the quote outlives the minute, and no candle is made of it.
    loaded("EURUSD", minutes(5));
    mount("EURUSD");
    act(() => {
      store.offer({ symbol: "EURUSD", bid: 1.2, ask: 1.2, mid: 1.2, ts: new Date().toISOString() });
      flushFrames();
    });
    expect(lwc.chart!.candles.update).not.toHaveBeenCalled();
    expect(drawn()).toEqual(minutes(5).map(candle));
  });

  it("still moves on a server that sends prices only", () => {
    loaded("EURUSD", minutes(5));
    mount("EURUSD");
    act(() => {
      store.applyQuote("EURUSD", 1.2, new Date((BASE + 5 * 60 + 1) * 1000).toISOString());
      store.offer({ symbol: "EURUSD", bid: 1.2, ask: 1.2, mid: 1.2, ts: new Date().toISOString() });
      flushFrames();
    });
    expect(updates().at(-1)).toMatchObject({ time: BASE + 300, open: 1.2, close: 1.2 });
  });
});

describe("the forming style", () => {
  it("draws the candle still forming unfinished, then plain when its bucket ends", () => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    vi.setSystemTime((BASE + 5 * 60 + 20) * 1000);
    loaded("EURUSD", minutes(5));
    mount("EURUSD");
    live("EURUSD", m(5));
    expect(updates().at(-1)!.color).toMatch(/66$/);

    // The minute ends with no tick to say so: a quiet market.
    act(() => void vi.advanceTimersByTime(60_000));
    expect(updates().at(-1)).toMatchObject({ time: BASE + 300 });
    expect(updates().at(-1)!.color).toBeUndefined();
  });

  it("draws a bucket that has already ended plainly", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime((BASE + 3_600) * 1000);
    loaded("EURUSD", minutes(5));
    mount("EURUSD");
    live("EURUSD", m(5));
    expect(updates().at(-1)!.color).toBeUndefined();
  });
});

describe("drawing as little as is correct", () => {
  it("switches timeframe at once, from minutes already held", () => {
    loaded("EURUSD", minutes(60));
    mount("EURUSD");
    pick("30m");
    expect(drawn()).toEqual(foldMinutes(minutes(60), 1_800).map(candle));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("adds a sealed minute with update(), not a full redraw", () => {
    loaded("EURUSD", minutes(10));
    mount("EURUSD");
    const redraws = lwc.chart!.candles.setData.mock.calls.length;
    act(() => store.sealMinute("EURUSD", m(10)));
    expect(lwc.chart!.candles.setData.mock.calls.length).toBe(redraws);
    expect(updates().at(-1)).toEqual(candle(m(10)));
  });

  it("does not redraw when the seal matches what the live candle already showed", () => {
    loaded("EURUSD", minutes(10));
    mount("EURUSD");
    live("EURUSD", m(10));
    live("EURUSD", m(11, { n: 1 }));
    const redraws = lwc.chart!.candles.setData.mock.calls.length;
    act(() => store.sealMinute("EURUSD", m(10)));
    expect(lwc.chart!.candles.setData.mock.calls.length).toBe(redraws);
  });

  it("redraws in full when a seal corrects a candle the chart has moved past", () => {
    loaded("EURUSD", minutes(10));
    mount("EURUSD");
    live("EURUSD", m(10));
    live("EURUSD", m(11, { n: 1 }));
    act(() => store.sealMinute("EURUSD", m(10, { h: 1.5 })));
    expect(drawn().at(-1)).toMatchObject({ time: BASE + 600, high: 1.5 });
  });

  it("fits the view once per pair and timeframe, keeping the user's zoom after that", () => {
    loaded("EURUSD", minutes(120));
    mount("EURUSD");
    expect(lwc.timeScale.fitContent).toHaveBeenCalledTimes(1);

    for (let i = 120; i < 125; i++) act(() => store.sealMinute("EURUSD", m(i)));
    act(() => store.mergeMinutes("EURUSD", minutes(30, -30, { src: "backfill", n: 0 }), "history"));
    expect(lwc.timeScale.fitContent).toHaveBeenCalledTimes(1);

    pick("5m");
    expect(lwc.timeScale.applyOptions).toHaveBeenLastCalledWith({ barSpacing: 8, rightOffset: 4 });
  });

  it("shows the newest few hundred candles of a long series, the rest a scroll away", () => {
    loaded("EURUSD", minutes(1_000));
    mount("EURUSD");
    expect(lwc.timeScale.setVisibleLogicalRange).toHaveBeenCalledWith({ from: 700, to: 1_002 });
    expect(drawn()).toHaveLength(1_000);
  });
});

describe("pairs never share a candle", () => {
  it("does not carry one symbol's OHLC into another's chart", () => {
    loaded("EURUSD", minutes(3));
    loaded("USDJPY", []);
    const view = mount("EURUSD");
    live("EURUSD", m(3));

    view.rerender(ui("USDJPY"));
    lwc.chart!.candles.update.mockClear();
    live("USDJPY", { t: BASE + 180, o: 157.21, h: 157.21, l: 157.21, c: 157.21, n: 1, src: "stream" });

    const got = updates().at(-1)!;
    for (const leg of ["open", "high", "low", "close"] as const) {
      expect(got[leg], `${leg} came from the previous symbol`).toBeCloseTo(157.21, 2);
    }
  });

  it("clears the previous pair's candles when the new pair has no history yet", () => {
    loaded("EURUSD", minutes(3));
    loaded("USDJPY", []);
    act(() =>
      store.pushAlert({
        s: "EURUSD",
        seq: 1,
        ts: new Date((BASE + 60) * 1000).toISOString(),
        old_regime: "normal",
        new_regime: "stressed",
        trigger_value: 3.4,
        threshold_value: 3,
        sigma: 0.0004,
        cause: "threshold",
        reason: "held",
      }),
    );
    const view = mount("EURUSD");
    expect(drawn()).toHaveLength(3);

    view.rerender(ui("USDJPY"));
    expect(drawn()).toEqual([]);
    expect(lwc.chart!.candles.setMarkers.mock.calls.at(-1)![0]).toEqual([]);
    expect(screen.getByText("Waiting for USDJPY history…")).toBeInTheDocument();
  });
});

describe("regime overlays", () => {
  const transition = (min: number, to: "normal" | "stressed"): Transition => ({
    s: "EURUSD",
    seq: min,
    ts: new Date((BASE + min * 60) * 1000).toISOString(),
    old_regime: to === "stressed" ? "normal" : "stressed",
    new_regime: to,
    trigger_value: 3.4,
    threshold_value: 3,
    sigma: 0.0004,
    cause: "threshold",
    reason: "held",
  });

  it("hands markers to the library oldest first", () => {
    // The store holds alerts newest first for the alert list; Lightweight Charts
    // requires ascending time and misplaces them silently otherwise.
    loaded("EURUSD", minutes(60));
    act(() => {
      store.pushAlert(transition(10, "stressed"));
      store.pushAlert(transition(30, "normal"));
    });
    mount("EURUSD");

    const markers = lwc.chart!.candles.setMarkers.mock.calls.at(-1)![0];
    expect(markers).toHaveLength(2);
    expect(markers.map((x: { time: number }) => x.time)).toEqual(
      [...markers.map((x: { time: number }) => x.time)].sort((a, b) => a - b),
    );
  });

  it("marks escalation and clear with different shapes, not only colours", () => {
    loaded("EURUSD", minutes(60));
    act(() => {
      store.pushAlert(transition(10, "stressed"));
      store.pushAlert(transition(30, "normal"));
    });
    mount("EURUSD");

    const shapes = lwc.chart!.candles.setMarkers.mock.calls
      .at(-1)![0]
      .map((x: { shape: string }) => x.shape);
    expect(new Set(shapes).size).toBe(2);
  });

  it("shades only the bars inside a stressed span", () => {
    loaded("EURUSD", minutes(60));
    act(() => {
      store.pushAlert(transition(20, "stressed"));
      store.pushAlert(transition(40, "normal"));
    });
    mount("EURUSD");

    // The shading series is created first so it paints behind the candles.
    const shade = lwc.chart!.histograms[0]!;
    const data = shade.setData.mock.calls.at(-1)![0] as { time: number; value: number }[];
    const hot = data.filter((d) => d.value === 1).map((d) => d.time);
    expect(hot[0]).toBe(BASE + 20 * 60);
    expect(hot.at(-1)).toBe(BASE + 39 * 60);
  });

  it("leaves an unresolved event shaded to the right edge", () => {
    loaded("EURUSD", minutes(60));
    act(() => store.pushAlert(transition(30, "stressed")));
    mount("EURUSD");

    const data = lwc.chart!.histograms[0]!.setData.mock.calls.at(-1)![0] as {
      time: number;
      value: number;
    }[];
    expect(data.at(-1)!.value).toBe(1);
  });
});

describe("lifecycle", () => {
  it("tears the chart down on unmount", () => {
    loaded("EURUSD", minutes(3));
    const view = mount("EURUSD");
    const created = lwc.chart!;
    view.unmount();
    expect(created.removed).toBe(true);
  });

  it("labels the canvas for screen readers", () => {
    loaded("EURUSD", minutes(3));
    const { container } = mount("EURUSD");
    expect(container.querySelector('[role="img"]')).toHaveAttribute(
      "aria-label",
      expect.stringContaining("EURUSD"),
    );
  });
});

describe("provenance", () => {
  /**
   * This replaced a full-width amber band drawn under every backfilled bar.
   * That was readable when a handful of minutes were backfilled, and became a
   * solid block across the whole chart once days of history were backfilled
   * behind a young feed — distinguishing nothing and burying the candles it sat
   * under. The boundary is the part worth showing.
   */
  const sources = (...srcs: string[]): Bar[] =>
    srcs.map((src, i) => m(i, { src, n: src === "backfill" ? 0 : 10 }));

  const markers = () => lwc.chart!.candles.setMarkers.mock.calls.at(-1)?.[0] ?? [];
  const boundary = () =>
    markers().find((x: { text?: string }) => /live from here/i.test(String(x.text)));

  it("marks where provider history ends and live data begins", () => {
    loaded("EURUSD", sources("backfill", "backfill", "stream"));
    mount("EURUSD");
    expect(boundary()).toBeDefined();
  });

  it("places it on the first live bar, not the last recorded one", () => {
    loaded("EURUSD", sources("backfill", "backfill", "stream"));
    mount("EURUSD");
    expect(boundary()?.time).toBe(BASE + 120);
  });

  it("says nothing when every bar is live", () => {
    loaded("EURUSD", sources("stream", "stream"));
    mount("EURUSD");
    expect(boundary()).toBeUndefined();
  });

  it("says nothing when every bar is provider history", () => {
    loaded("EURUSD", sources("backfill", "backfill"));
    mount("EURUSD");
    expect(boundary()).toBeUndefined();
  });

  it("states the proportion in text", () => {
    loaded("EURUSD", sources("backfill", "backfill", "stream"));
    mount("EURUSD");
    expect(screen.getByText(/2 of 3 bars from provider history/i)).toBeInTheDocument();
  });

  it("calls a window of provider history what it is", () => {
    loaded("EURUSD", sources("backfill", "backfill"));
    mount("EURUSD");
    expect(screen.getByText(/^provider history$/i)).toBeInTheDocument();
  });

  it("calls a candle the stream joined part-way through what it is", () => {
    // 10:00-10:02 provider history, 10:03-10:05 live: on 5m the first candle
    // holds both. It used to be called provider history, after its first minute.
    loaded("EURUSD", sources("backfill", "backfill", "backfill", "stream", "stream", "stream"));
    mount("EURUSD");
    pick("5m");
    expect(screen.getByText(/^1 of 2 bars part provider, part live$/i)).toBeInTheDocument();
    expect(boundary()?.time).toBe(BASE);
  });

  it("keeps markers ascending once the boundary is merged in", () => {
    // The alert sits inside the backfilled stretch, earlier than the boundary:
    // prepending a late boundary to an early alert yields a descending list.
    loaded("EURUSD", sources(...Array.from({ length: 40 }, (_, i) => (i < 30 ? "backfill" : "stream"))));
    act(() =>
      store.pushAlert({
        s: "EURUSD",
        seq: 10,
        ts: new Date((BASE + 600) * 1000).toISOString(),
        old_regime: "normal",
        new_regime: "stressed",
        trigger_value: 3.4,
        threshold_value: 3,
        sigma: 0.0004,
        cause: "threshold",
        reason: "held",
      }),
    );
    mount("EURUSD");

    const times = markers().map((x: { time: number }) => x.time);
    expect(times.length).toBeGreaterThanOrEqual(2);
    expect(times).toEqual([...times].sort((a: number, b: number) => a - b));
  });

  it("says when today's candle is built from fewer minutes than the market was open", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime((BASE + 10 * 60) * 1000);
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => ({ bars: [] }) } as Response);
    // 00:00-09:59 unseen, 10:00-10:09 held: 10 of 610 minutes.
    loaded("EURUSD", minutes(10));
    mount("EURUSD");
    pick("1d");
    expect(
      await screen.findByText("Today partial: 10 of 610 minutes observed"),
    ).toBeInTheDocument();
  });
});

describe("the daily rollover at 00:00 UTC", () => {
  const D = BASE - 10 * 3_600; // 2026-10-21 00:00 UTC
  const NEXT = D + 86_400;
  const PAIRS: Record<string, number> = { EURUSD: 1, GBPUSD: 1.2, USDJPY: 140, AUDUSD: 0.65, USDCHF: 0.76 };
  const scaled = (b: Bar, k: number): Bar => ({ ...b, o: b.o * k, h: b.h * k, l: b.l * k, c: b.c * k });

  for (const [pair, k] of Object.entries(PAIRS)) {
    it(`${pair}: closes the day as observed, opens the next with its first tick, keeps both`, async () => {
      vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
      vi.setSystemTime((NEXT - 30) * 1000); // 23:59:30
      // 10:00-23:58 sealed; the provider's bar for the day as its 23:05 refresh left it.
      const day = minutes(839).map((b) => scaled(b, k));
      const seen = day.filter((b) => b.t < D + 23 * 3_600 + 5 * 60);
      const provider: Bar = {
        t: D,
        o: day[0]!.o * 0.999,
        h: Math.max(...seen.map((b) => b.h)) * 1.0004,
        l: Math.min(...seen.map((b) => b.l)) * 0.9999,
        c: seen.at(-1)!.c,
        n: 0,
        src: "backfill",
      };
      vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => ({ bars: [provider] }) } as Response);
      loaded(pair, day);
      mount(pair);
      pick("1d");
      await act(async () => {
        for (let i = 0; i < 5; i++) await Promise.resolve();
      });
      expect(fx().store.daily(pair)).toHaveLength(1);

      const lastMinute = scaled(m(839), k);
      live(pair, lastMinute);
      const zoomCalls = zoom();

      // 00:00:05 - the first tick of the new day.
      vi.setSystemTime((NEXT + 5) * 1000);
      const first = scaled(m(840, { n: 1 }), k);
      live(pair, first);

      const closedAs = {
        time: D,
        open: provider.o, // our minutes began at 10:00, so the provider saw the open
        high: Math.max(provider.h, ...day.map((b) => b.h), lastMinute.h),
        low: Math.min(provider.l, ...day.map((b) => b.l), lastMinute.l),
        close: lastMinute.c, // we saw 23:59, the day's last minute
      };
      const settled = updates().filter((p) => p.time === D).at(-1)!;
      expect(settled).toMatchObject(closedAs);
      expect(settled.color).toBeUndefined();
      expect(updates().at(-1)).toMatchObject({ time: NEXT, open: first.o, close: first.c });

      // The minute clock ticks over into the new day and the series re-renders:
      // whatever it redraws, the finished day stays as it closed.
      const fullDraws = lwc.chart!.candles.setData.mock.calls.length;
      const partDraws = updates().length;
      await act(async () => void vi.advanceTimersByTime(20_000));
      const redrawn = lwc.chart!.candles.setData.mock.calls
        .slice(fullDraws)
        .map((c) => (c[0] as Point[]).find((p) => p.time === D));
      for (const p of redrawn) expect(p).toMatchObject(closedAs);
      for (const p of updates().slice(partDraws).filter((u) => u.time === D)) expect(p).toMatchObject(closedAs);

      // A reconnect a moment after midnight: the snapshot holds 23:59 sealed.
      act(() => fx().store.mergeMinutes(pair, [...day.slice(-3), { ...lastMinute, n: 9 }]));
      const times = (lwc.chart!.candles.setData.mock.calls.at(-1)![0] as Point[]).map((p) => p.time);
      expect(times).toEqual([...new Set(times)].sort((a, b) => a - b));
      expect(zoom()).toEqual(zoomCalls);
    });
  }
});
