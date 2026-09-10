import { vi } from "vitest";

/** Recording time scale. `applyOptions` is what the chart uses to pin bar width
 *  while a session is still short; without it here the component throws. */
export const timeScale = {
  fitContent: vi.fn(),
  applyOptions: vi.fn(),
  setVisibleRange: vi.fn(),
  scrollToRealTime: vi.fn(),
};

/**
 * A stand-in for Lightweight Charts.
 *
 * The library is canvas-only and jsdom has no canvas, so every test that renders
 * a chart either mocks this or skips the component entirely — which is how
 * `PriceChart` reached 0% coverage while containing the live-bar merge logic,
 * the marker ordering rule and the regime shading. Recording the calls lets the
 * *logic* be tested here and leaves the *pixels* to the browser suite.
 */

export interface SeriesSpy {
  setData: ReturnType<typeof vi.fn>;
  update: ReturnType<typeof vi.fn>;
  setMarkers: ReturnType<typeof vi.fn>;
  applyOptions: ReturnType<typeof vi.fn>;
  priceScale: () => { applyOptions: ReturnType<typeof vi.fn> };
}

export interface ChartSpy {
  candles: SeriesSpy;
  histograms: SeriesSpy[];
  removed: boolean;
}

const series = (): SeriesSpy => ({
  setData: vi.fn(),
  update: vi.fn(),
  setMarkers: vi.fn(),
  applyOptions: vi.fn(),
  priceScale: () => ({ applyOptions: vi.fn() }),
});

/** The most recent chart created. Charts are per-mount, so this is the one. */
export let chart: ChartSpy | null = null;

export function createChart(): unknown {
  const spy: ChartSpy = { candles: series(), histograms: [], removed: false };
  chart = spy;
  return {
    addCandlestickSeries: () => spy.candles,
    addHistogramSeries: () => {
      const h = series();
      spy.histograms.push(h);
      return h;
    },
    // The regime shading moved from a histogram to a stepped area: a histogram
    // leaves a gap between bars, which at 300 candles reads as a barcode over
    // the chart. Collected in the same list so existing assertions about "the
    // shading series" keep working regardless of which primitive draws it.
    addAreaSeries: () => {
      const a = series();
      spy.histograms.push(a);
      return a;
    },
    applyOptions: vi.fn(),
    // One shared object, not a fresh one per call: the component calls
    // timeScale() again on each render, and a new mock each time would discard
    // the very calls a test is trying to assert on.
    timeScale: () => timeScale,
    remove: () => {
      spy.removed = true;
    },
  };
}

export function reset(): void {
  chart = null;
}
