import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as lwc from "../../test/lwc";

vi.mock("lightweight-charts", () => ({ createChart: lwc.createChart }));

import { flushFrames } from "../../test/setup";
import { StoreCtx } from "../../lib/stream/hooks";
import { MarketStore } from "../../lib/stream/store";
import type { Bar, Quote, Transition } from "../../lib/stream/types";
import { ThemeProvider } from "../../lib/theme";
import { PriceChart } from "../charts/PriceChart";

const T0 = 1_787_000_000; // a minute boundary
let store: MarketStore;

beforeEach(() => {
  store = new MarketStore();
  lwc.reset();
});

const bars = (n: number, px: number, from = T0): Bar[] =>
  Array.from({ length: n }, (_, i) => ({
    t: from + i * 60,
    o: px,
    h: px,
    l: px,
    c: px,
    n: 10,
    src: "stream",
  }));

const quote = (symbol: string, mid: number, minute: number): Quote => ({
  symbol,
  bid: mid,
  ask: mid,
  mid,
  ts: new Date((T0 + minute * 60 + 5) * 1000).toISOString(),
});

const mount = (symbol: string) =>
  render(
    <ThemeProvider>
      <StoreCtx.Provider value={store}>
        <PriceChart symbol={symbol} />
      </StoreCtx.Provider>
    </ThemeProvider>,
  );

const tick = (q: Quote) =>
  act(() => {
    store.offer(q);
    flushFrames();
  });

describe("the in-flight bar", () => {
  it("does not carry one symbol's OHLC into another's chart", () => {
    /**
     * The defect. Switching to a pair with no cached bars left the previous
     * pair's in-flight bar in place — the snapshot effect returns early on an
     * empty series and never cleared it — so the next tick merged a EURUSD
     * open, high and low with a USDJPY close and drew it as a candle.
     *
     * A currency pair trading near 1.08 and one trading near 157 make the
     * corruption obvious; at similar price levels it would just be quietly wrong.
     */
    act(() => store.setBars({ EURUSD: bars(3, 1.0842) }));
    const view = mount("EURUSD");
    tick(quote("EURUSD", 1.0842, 3));

    view.rerender(
      <ThemeProvider>
        <StoreCtx.Provider value={store}>
          <PriceChart symbol="USDJPY" />
        </StoreCtx.Provider>
      </ThemeProvider>,
    );

    lwc.chart!.candles.update.mockClear();
    tick(quote("USDJPY", 157.21, 3));

    const drawn = lwc.chart!.candles.update.mock.calls.at(-1)?.[0];
    expect(drawn).toBeDefined();
    // Every leg of the candle must belong to USDJPY.
    for (const leg of ["open", "high", "low", "close"] as const) {
      expect(drawn[leg], `${leg} came from the previous symbol`).toBeCloseTo(157.21, 2);
    }
  });

  it("merges ticks within a minute and opens a new bar across the boundary", () => {
    act(() => store.setBars({ EURUSD: bars(2, 1.08) }));
    mount("EURUSD");

    tick(quote("EURUSD", 1.09, 2));
    tick(quote("EURUSD", 1.11, 2));
    tick(quote("EURUSD", 1.07, 2));
    const merged = lwc.chart!.candles.update.mock.calls.at(-1)![0];
    expect(merged.high).toBeCloseTo(1.11, 5);
    expect(merged.low).toBeCloseTo(1.07, 5);
    expect(merged.close).toBeCloseTo(1.07, 5);

    tick(quote("EURUSD", 1.1, 3));
    const fresh = lwc.chart!.candles.update.mock.calls.at(-1)![0];
    expect(fresh.time).toBeGreaterThan(merged.time);
    expect(fresh.open).toBeCloseTo(1.1, 5);
    expect(fresh.high).toBeCloseTo(1.1, 5);
  });
});

describe("regime overlays", () => {
  const transition = (min: number, to: "normal" | "stressed"): Transition => ({
    s: "EURUSD",
    seq: min,
    ts: new Date((T0 + min * 60) * 1000).toISOString(),
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
    act(() => {
      store.setBars({ EURUSD: bars(60, 1.08) });
      store.pushAlert(transition(10, "stressed"));
      store.pushAlert(transition(30, "normal"));
    });
    mount("EURUSD");

    const markers = lwc.chart!.candles.setMarkers.mock.calls.at(-1)![0];
    expect(markers).toHaveLength(2);
    expect(markers.map((m: { time: number }) => m.time)).toEqual(
      [...markers.map((m: { time: number }) => m.time)].sort((a, b) => a - b),
    );
  });

  it("marks escalation and clear with different shapes, not only colours", () => {
    act(() => {
      store.setBars({ EURUSD: bars(60, 1.08) });
      store.pushAlert(transition(10, "stressed"));
      store.pushAlert(transition(30, "normal"));
    });
    mount("EURUSD");

    const shapes = lwc.chart!.candles.setMarkers.mock.calls
      .at(-1)![0]
      .map((m: { shape: string }) => m.shape);
    expect(new Set(shapes).size).toBe(2);
  });

  it("shades only the bars inside a stressed span", () => {
    act(() => {
      store.setBars({ EURUSD: bars(60, 1.08) });
      store.pushAlert(transition(20, "stressed"));
      store.pushAlert(transition(40, "normal"));
    });
    mount("EURUSD");

    // The shading series is created first so it paints behind the candles.
    const shade = lwc.chart!.histograms[0]!;
    const data = shade.setData.mock.calls.at(-1)![0] as { time: number; value: number }[];
    const hot = data.filter((d) => d.value === 1).map((d) => d.time);
    expect(hot[0]).toBe(T0 + 20 * 60);
    expect(hot.at(-1)).toBe(T0 + 39 * 60);
  });

  it("leaves an unresolved event shaded to the right edge", () => {
    act(() => {
      store.setBars({ EURUSD: bars(60, 1.08) });
      store.pushAlert(transition(30, "stressed"));
    });
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
    act(() => store.setBars({ EURUSD: bars(3, 1.08) }));
    const view = mount("EURUSD");
    const created = lwc.chart!;
    view.unmount();
    expect(created.removed).toBe(true);
  });

  it("labels the canvas for screen readers", () => {
    act(() => store.setBars({ EURUSD: bars(3, 1.08) }));
    const { container } = mount("EURUSD");
    expect(container.querySelector('[role="img"]')).toHaveAttribute(
      "aria-label",
      expect.stringContaining("EURUSD"),
    );
  });
});
